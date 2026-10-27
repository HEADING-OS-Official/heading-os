#!/usr/bin/env python3
"""Porkbun API from the terminal, and delivery of a Porkbun-issued certificate.

Porkbun issues and renews a free Let's Encrypt certificate for a domain whose DNS
it hosts, and `/ssl/retrieve/<domain>` hands out the current chain and key. This
script is the one place the workspace talks to that API:

    porkbun.py ping
    porkbun.py dns list <domain>
    porkbun.py dns get <domain> <type> [<subdomain>]
    porkbun.py dns set <domain> <type> <subdomain> <content> [--ttl N] [--yes]
    porkbun.py dns delete <domain> <type> <subdomain> [--yes]
    porkbun.py ssl show <domain>
    porkbun.py ssl push [<domain>] [--host ALIAS] [--name FQDN] [--force]
    porkbun.py self-test

Credentials come from the engine's gitignored `.env` (`PORKBUN_API_KEY`,
`PORKBUN_SECRET_API_KEY`) through `load_api_key` and are never printed. A write to
DNS without `--yes` only prints what it would do.

`ssl push` keeps the API key on this machine. It retrieves the bundle, validates
it here, asks the server what it holds, and hands the chain and key over SSH to a
forced command on the server (`scripts/server/install-tls-cert.sh`), which
validates again, swaps the files atomically and reloads nginx only after
`nginx -t` passes. The server never holds a key that can edit the zone. Its
defaults, so that the timer unit carries no name, come from `.env`:
`PORKBUN_CERT_DOMAIN`, `PORKBUN_CERT_HOST` (an ssh alias) and `PORKBUN_CERT_NAME`
(the FQDN the certificate must cover). A failed push, or a certificate on the
server with fewer than `ALERT_DAYS` left, is reported to the operator's own
notification sinks and exits non-zero, so the unit shows `failed`.

Certificate inspection shells out to `openssl` with list arguments, so the
script adds no Python dependency. `self-test` exercises the validation on
certificates generated in memory and needs no network.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.api import load_api_key  # noqa: E402
from scripts.utils.telegram_notify import notify, own_targets  # noqa: E402
from scripts.utils.user_agent import user_agent  # noqa: E402

API = "https://api.porkbun.com/api/json/v3"
TIMEOUT_S = 30
SSH_TIMEOUT_S = 120
ALERT_DAYS = 20


class PorkbunError(RuntimeError):
    """The API answered with an error, or could not be reached."""


class CertError(ValueError):
    """A certificate bundle failed validation."""


# --------------------------------------------------------------------------- API

def call(path: str, extra: dict | None = None) -> dict:
    """POST to the Porkbun API with the credentials and return the JSON body."""
    body = {
        "apikey": load_api_key("PORKBUN_API_KEY"),
        "secretapikey": load_api_key("PORKBUN_SECRET_API_KEY"),
        **(extra or {}),
    }
    try:
        resp = requests.post(
            f"{API}/{path.lstrip('/')}",
            json=body,
            headers={"User-Agent": user_agent()},
            timeout=TIMEOUT_S,
        )
    except requests.exceptions.RequestException as exc:
        raise PorkbunError(f"{path}: {type(exc).__name__}: {exc}") from exc
    try:
        data = resp.json()
    except ValueError as exc:
        raise PorkbunError(f"{path}: HTTP {resp.status_code}, not JSON: {resp.text[:200]}") from exc
    # Porkbun reports its errors as JSON with a non-2xx code; keep its message.
    if resp.status_code != 200 or data.get("status") != "SUCCESS":
        raise PorkbunError(f"{path}: HTTP {resp.status_code}: {data.get('message') or data}")
    return data


# ------------------------------------------------------------------ certificates

@dataclass
class CertInfo:
    subject: str
    names: list[str]
    not_before: datetime
    not_after: datetime
    fingerprint: str

    def days_left(self, now: datetime) -> float:
        return (self.not_after - now).total_seconds() / 86400


def _openssl(args: list[str], stdin: str) -> str:
    proc = subprocess.run(["openssl", *args], input=stdin, capture_output=True, text=True)
    if proc.returncode != 0:
        raise CertError(f"openssl {args[0]}: {proc.stderr.strip()[:300]}")
    return proc.stdout


def _openssl_date(value: str) -> datetime:
    # openssl prints e.g. "Dec 28 14:06:55 2026 GMT"; a space in the format also
    # matches the double space openssl uses before a one-digit day.
    return datetime.strptime(value.strip(), "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)


def split_pem(text: str) -> tuple[list[str], str]:
    """Return (certificates in order, private key) from a PEM stream."""
    certs, key, block = [], "", []
    for line in text.splitlines():
        if line.startswith("-----BEGIN"):
            block = [line]
        elif block:
            block.append(line)
            if line.startswith("-----END"):
                pem = "\n".join(block) + "\n"
                if "CERTIFICATE" in line:
                    certs.append(pem)
                elif "PRIVATE KEY" in line:
                    if key:
                        raise CertError("more than one private key in the bundle")
                    key = pem
                block = []
    return certs, key


def inspect_cert(pem: str) -> CertInfo:
    out = _openssl(
        ["x509", "-noout", "-subject", "-startdate", "-enddate", "-fingerprint",
         "-sha256", "-ext", "subjectAltName"],
        pem,
    )
    fields, names = {}, []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("DNS:"):
            names = [n.strip()[4:] for n in line.split(",") if n.strip().startswith("DNS:")]
        elif "=" in line:
            k, v = line.split("=", 1)
            fields[k.strip().lower()] = v.strip()
    fingerprint = next((v for k, v in fields.items() if "fingerprint" in k), "")
    return CertInfo(
        subject=fields.get("subject", ""),
        names=names,
        not_before=_openssl_date(fields["notbefore"]),
        not_after=_openssl_date(fields["notafter"]),
        fingerprint=fingerprint,
    )


def covers(pattern: str, name: str) -> bool:
    """True when a certificate name (possibly `*.x`) covers one host name."""
    pattern, name = pattern.lower().rstrip("."), name.lower().rstrip(".")
    if pattern == name:
        return True
    if pattern.startswith("*."):
        head, _, rest = name.partition(".")
        return bool(head) and rest == pattern[2:]
    return False


def validate_bundle(chain: str, key: str, name: str, now: datetime) -> CertInfo:
    """Check a chain and key before anything installs them; return the leaf info."""
    certs, _ = split_pem(chain)
    if not certs:
        raise CertError("no certificate in the chain")
    if "PRIVATE KEY" not in key:
        raise CertError("no private key")
    leaf = inspect_cert(certs[0])
    if not any(covers(n, name) for n in leaf.names):
        raise CertError(f"certificate names {leaf.names} do not cover {name}")
    if now < leaf.not_before:
        raise CertError(f"certificate not valid before {leaf.not_before:%Y-%m-%d}")
    if now >= leaf.not_after:
        raise CertError(f"certificate expired {leaf.not_after:%Y-%m-%d}")
    cert_pub = _openssl(["x509", "-noout", "-pubkey"], certs[0])
    key_pub = _openssl(["pkey", "-pubout"], key)
    if cert_pub.strip() != key_pub.strip():
        raise CertError("private key does not match the certificate")
    return leaf


# -------------------------------------------------------------------- commands

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _setting(name: str, given: str | None) -> str:
    value = given or load_api_key(name, required=False)
    if not value:
        raise SystemExit(f"porkbun: {name} is not set; pass it as an argument or put it in .env")
    return value


def cmd_ping(_args) -> int:
    data = call("ping")
    print(f"SUCCESS, request came from {data.get('yourIp')}")
    return 0


def cmd_dns_list(args) -> int:
    for r in call(f"dns/retrieve/{args.domain}").get("records", []):
        print(f"{r['name']}\t{r['type']}\t{r['ttl']}\t{r['content']}")
    return 0


def cmd_dns_get(args) -> int:
    path = f"dns/retrieveByNameType/{args.domain}/{args.type.upper()}/{args.subdomain or ''}".rstrip("/")
    records = call(path).get("records", [])
    for r in records:
        print(f"{r['name']}\t{r['type']}\t{r['ttl']}\t{r['content']}")
    return 0 if records else 1


def cmd_dns_set(args) -> int:
    rtype, sub = args.type.upper(), args.subdomain
    existing = call(f"dns/retrieveByNameType/{args.domain}/{rtype}/{sub}").get("records", [])
    action = "edit" if existing else "create"
    fqdn = f"{sub}.{args.domain}" if sub else args.domain
    print(f"{action}: {fqdn} {rtype} {args.content} ttl {args.ttl}"
          + (f" (was {', '.join(r['content'] for r in existing)})" if existing else ""))
    if not args.yes:
        print("dry run; add --yes to apply")
        return 2
    if existing:
        call(f"dns/editByNameType/{args.domain}/{rtype}/{sub}", {"content": args.content, "ttl": str(args.ttl)})
    else:
        call(f"dns/create/{args.domain}", {"name": sub, "type": rtype, "content": args.content, "ttl": str(args.ttl)})
    print("done")
    return 0


def cmd_dns_delete(args) -> int:
    rtype, sub = args.type.upper(), args.subdomain
    existing = call(f"dns/retrieveByNameType/{args.domain}/{rtype}/{sub}").get("records", [])
    if not existing:
        print("no such record")
        return 1
    for r in existing:
        print(f"delete: {r['name']} {r['type']} {r['content']}")
    if not args.yes:
        print("dry run; add --yes to apply")
        return 2
    call(f"dns/deleteByNameType/{args.domain}/{rtype}/{sub}")
    print("done")
    return 0


def cmd_ssl_show(args) -> int:
    certs, _ = split_pem(call(f"ssl/retrieve/{args.domain}").get("certificatechain", ""))
    if not certs:
        raise PorkbunError("ssl/retrieve returned no certificate")
    leaf, now = inspect_cert(certs[0]), _now()
    print(f"subject:     {leaf.subject}")
    print(f"names:       {', '.join(leaf.names)}")
    print(f"valid:       {leaf.not_before:%Y-%m-%d %H:%M} .. {leaf.not_after:%Y-%m-%d %H:%M} UTC"
          f" ({leaf.days_left(now):.0f} days left)")
    print(f"sha256:      {leaf.fingerprint}")
    print(f"chain:       {len(certs)} certificate(s)")
    return 0


def _ssh(host: str, mode: str, stdin: str = "") -> str:
    # The server side is a forced command: `mode` arrives as SSH_ORIGINAL_COMMAND
    # and is checked against a two-word allowlist there.
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", host, mode],
        input=stdin, capture_output=True, text=True, timeout=SSH_TIMEOUT_S,
    )
    if proc.returncode != 0:
        raise PorkbunError(f"ssh {host} {mode}: exit {proc.returncode}: {proc.stderr.strip()[:300]}")
    return proc.stdout


def _server_status(host: str) -> dict:
    status = {}
    for line in _ssh(host, "status").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            status[k.strip()] = v.strip()
    return status


def _alarm(subject: str, detail: str) -> None:
    print(f"porkbun: {subject}\n{detail}", file=sys.stderr)
    text = f"HEADING OS certificate: {subject}\n\n{detail}"
    delivered = sum(1 for t in sorted(own_targets()) if notify(t, text))
    if not delivered:
        print("porkbun: NO NOTIFICATION SENT, no operator sink is configured or the"
              " transport refused.", file=sys.stderr)


def cmd_ssl_push(args) -> int:
    domain = _setting("PORKBUN_CERT_DOMAIN", args.domain)
    host = _setting("PORKBUN_CERT_HOST", args.host)
    name = _setting("PORKBUN_CERT_NAME", args.name)
    now = _now()
    try:
        bundle = call(f"ssl/retrieve/{domain}")
        chain, key = bundle.get("certificatechain", ""), bundle.get("privatekey", "")
        leaf = validate_bundle(chain, key, name, now)
        installed = _server_status(host)
        if installed.get("fingerprint") == leaf.fingerprint:
            left = leaf.days_left(now)
            print(f"up to date: {leaf.fingerprint[:23]}..., {left:.0f} days left")
            if left < ALERT_DAYS:
                _alarm(f"{name} expires in {left:.0f} days",
                       f"Porkbun has not issued a newer certificate for {domain} yet; the"
                       f" installed one ends {leaf.not_after:%Y-%m-%d}.")
                return 1
            return 0
        if installed.get("not_after") and not args.force:
            current_end = datetime.fromisoformat(installed["not_after"])
            if leaf.not_after <= current_end:
                raise CertError(
                    f"Porkbun's certificate ends {leaf.not_after:%Y-%m-%d}, not later than the"
                    f" installed one ({current_end:%Y-%m-%d}); refusing to downgrade (--force)"
                )
        print(_ssh(host, "install", stdin=chain.rstrip("\n") + "\n" + key).strip())
        after = _server_status(host)
        if after.get("fingerprint") != leaf.fingerprint:
            raise PorkbunError(f"server reports {after.get('fingerprint')} after install")
        print(f"installed: {leaf.fingerprint[:23]}..., valid to {leaf.not_after:%Y-%m-%d}")
        return 0
    except (PorkbunError, CertError, ValueError, subprocess.TimeoutExpired) as exc:
        _alarm(f"push to {name} failed", str(exc))
        return 1


# ------------------------------------------------------------------- self-test

def _make_cert(key: str, cn: str, san: str, days: int) -> str:
    return _openssl(
        ["req", "-x509", "-key", "/dev/stdin", "-subj", f"/CN={cn}",
         "-addext", f"subjectAltName={san}", "-days", str(days)],
        key,
    )


def cmd_self_test(_args) -> int:
    """Exercise the validation on throwaway certificates; no network, no files."""
    key_a = _openssl(["genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256"], "")
    key_b = _openssl(["genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256"], "")
    wild = _make_cert(key_a, "example.test", "DNS:*.example.test,DNS:example.test", 90)
    now = _now()
    cases = [
        ("wildcard covers one label", lambda: validate_bundle(wild, key_a, "memory.example.test", now), True),
        ("apex name covered", lambda: validate_bundle(wild, key_a, "example.test", now), True),
        ("wildcard does not cover two labels", lambda: validate_bundle(wild, key_a, "a.b.example.test", now), False),
        ("other domain refused", lambda: validate_bundle(wild, key_a, "memory.example.org", now), False),
        ("mismatched key refused", lambda: validate_bundle(wild, key_b, "memory.example.test", now), False),
        ("expired refused", lambda: validate_bundle(wild, key_a, "memory.example.test", now + timedelta(days=91)), False),
        ("not yet valid refused", lambda: validate_bundle(wild, key_a, "memory.example.test", now - timedelta(days=1)), False),
        ("missing key refused", lambda: validate_bundle(wild, "", "memory.example.test", now), False),
        ("empty chain refused", lambda: validate_bundle("", key_a, "memory.example.test", now), False),
    ]
    failures = 0
    for label, fn, should_pass in cases:
        try:
            fn()
            ok = should_pass
        except CertError as exc:
            ok = not should_pass
            label += f" ({exc})" if not should_pass else f" UNEXPECTED: {exc}"
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {label}")
    certs, key = split_pem(wild + key_a)
    split_ok = len(certs) == 1 and key.strip() == key_a.strip()
    failures += not split_ok
    print(f"{'ok  ' if split_ok else 'FAIL'} chain and key split from one stream")
    print(f"{len(cases) + 1 - failures}/{len(cases) + 1} passed")
    return 1 if failures else 0


# ------------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Porkbun API and certificate delivery.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ping").set_defaults(fn=cmd_ping)
    sub.add_parser("self-test").set_defaults(fn=cmd_self_test)

    dns = sub.add_parser("dns").add_subparsers(dest="dns_cmd", required=True)
    d = dns.add_parser("list"); d.add_argument("domain"); d.set_defaults(fn=cmd_dns_list)
    d = dns.add_parser("get"); d.add_argument("domain"); d.add_argument("type")
    d.add_argument("subdomain", nargs="?", default=""); d.set_defaults(fn=cmd_dns_get)
    d = dns.add_parser("set"); d.add_argument("domain"); d.add_argument("type")
    d.add_argument("subdomain"); d.add_argument("content")
    d.add_argument("--ttl", type=int, default=600); d.add_argument("--yes", action="store_true")
    d.set_defaults(fn=cmd_dns_set)
    d = dns.add_parser("delete"); d.add_argument("domain"); d.add_argument("type")
    d.add_argument("subdomain"); d.add_argument("--yes", action="store_true")
    d.set_defaults(fn=cmd_dns_delete)

    ssl = sub.add_parser("ssl").add_subparsers(dest="ssl_cmd", required=True)
    s = ssl.add_parser("show"); s.add_argument("domain"); s.set_defaults(fn=cmd_ssl_show)
    s = ssl.add_parser("push"); s.add_argument("domain", nargs="?")
    s.add_argument("--host", help="ssh alias of the server (PORKBUN_CERT_HOST)")
    s.add_argument("--name", help="FQDN the certificate must cover (PORKBUN_CERT_NAME)")
    s.add_argument("--force", action="store_true", help="install even if not newer")
    s.set_defaults(fn=cmd_ssl_push)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except (PorkbunError, CertError, ValueError) as exc:
        print(f"porkbun: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
