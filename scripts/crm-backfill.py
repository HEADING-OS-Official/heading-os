#!/usr/bin/env python3
"""crm-backfill.py -- reconcile the CRM with a mail metadata export.

Reads the JSON-lines file `sync-exchange.py --export-metadata` writes (date,
direction, sender, To, CC, subject; no bodies), matches every address against
the contact cards, and reports, or with --apply writes:

  - last_touch moved FORWARD to the newest mail day, never back;
  - one summary line at the top of the card's Interaction Log, in the card's
    own "- YYYY-MM-DD | Email | ..." form;
  - "AWAITING REPLY" on that line when the newest inbound mail is newer than
    the newest outbound one AND not older than the card's last_touch, because
    a later touch on another channel already answered it.

Matching is strict and exact on the card's `email` and `secondary_email` and
the address-book entity's `canonical_email` and `other_emails`. Inbound mail
counts only when the operator is in To; CC never counts, in either direction.

It also reports what it cannot decide: correspondents missing from the CRM
(external and corporate-domain separately, bulk and no-reply senders
filtered), and unknown addresses whose display name is an existing contact's
name, which look like that contact's second address. Neither is written unless
the operator lists it in an --approved file:

    {"aliases": {"second@address": "existing-slug"},
     "new": [{"slug": "...", "name": "...", "email": "...", "employer": "...",
              "type": "prospect", "owner": "<only when the type has no row>",
              "title": "", "region": "", "timezone": ""}]}

A new contact's canonical owner comes from the owner table in `crm-config`
(`parse_owner_map`); an entry whose type has no row and that names no owner is
refused, never stamped with a placeholder.

Usage:
  .venv/bin/python scripts/sync-exchange.py --export-metadata mail.jsonl --since 2026-04-16
  .venv/bin/python scripts/crm-backfill.py --mail mail.jsonl                    # report only
  .venv/bin/python scripts/crm-backfill.py --mail mail.jsonl --approved ok.json # report incl. approved
  .venv/bin/python scripts/crm-backfill.py --mail mail.jsonl --approved ok.json --apply
"""

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils import crm as C  # noqa: E402
from scripts.utils.crm_autolog import atomic_write, bump_last_touch_in_text, data_is_readonly  # noqa: E402
from scripts.utils.markdown import frontmatter_date, frontmatter_list, parse_frontmatter, set_frontmatter_field  # noqa: E402
from scripts.utils.operator_identity import corporate_email_domain, get_operator, operator_slug  # noqa: E402
from scripts.utils.workspace import get_default_tz, load_env  # noqa: E402

BULK = re.compile(
    r"(no-?reply|donotreply|do-not-reply|mailer-daemon|postmaster|notification|notify|newsletter|news@|info@"
    r"|marketing|bounce|alerts?@|digest|updates?@|support@|hello@|team@|calendar|linkedin|zoom\.us|microsoft"
    r"|google\.com|accounts?@|billing|invoice|receipts?@|events?@|webinar|mailchimp|hubspot|salesforce"
    r"|sendgrid|amazonses|atlassian|github|gitlab|slack|imip\.me\.com)", re.I)
TAG = "Exchange back-fill"


# ============================================================
# Inputs
# ============================================================

def local_day(stamp: str) -> str:
    """The configured timezone's calendar day for an ISO timestamp; naive means UTC."""
    dt = datetime.fromisoformat(stamp)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(get_default_tz()).date().isoformat()


def load_mail(path: Path) -> list:
    rows = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.startswith("{"):
            continue
        m = json.loads(line)
        if not m.get("date"):
            print(f"crm-backfill: line {n} has no date; skipped", file=sys.stderr)
            continue
        m["day"] = local_day(m["date"])
        rows.append(m)
    return rows


def self_addresses(extra: list) -> set:
    load_env()
    found = {os.getenv("EXCHANGE_EMAIL", ""), get_operator().get("email", "")} | set(extra or [])
    return {a.strip().lower() for a in found if a and a.strip()}


def _norm_name(s: str) -> str:
    return " ".join(str(s or "").lower().replace('"', "").split())


def load_cards():
    """(cards, index, conflicts): slug -> card info, address -> slug, address -> [slugs]."""
    home, ab = C.contacts_home(), C.address_book_home()
    cards, owners = {}, defaultdict(set)
    for path in sorted(home.glob("*.md")):
        if not C.is_contact_file(path):
            continue
        text = path.read_text(encoding="utf-8")
        fm, _ = parse_frontmatter(text)
        if not fm:
            continue
        ent, ent_path = {}, None
        if fm.get("entity_ref"):
            ent_path = ab / f"{fm['entity_ref']}.md"
            if ent_path.exists():
                ent, _ = parse_frontmatter(ent_path.read_text(encoding="utf-8"))
        addrs = {str(fm.get(k) or "") for k in ("email", "secondary_email")}
        addrs.add(str((ent or {}).get("canonical_email") or ""))
        addrs |= set(frontmatter_list((ent or {}).get("other_emails")))
        slug = path.stem
        cards[slug] = {"path": path, "fm": fm, "entity_path": ent_path,
                       "name": (ent or {}).get("name") or fm.get("name") or ""}
        for a in addrs:
            if a.strip():
                owners[a.strip().lower()].add(slug)
    index = {a: next(iter(s)) for a, s in owners.items() if len(s) == 1}
    conflicts = {a: sorted(s) for a, s in owners.items() if len(s) > 1}
    return cards, index, conflicts


def stored_last_touch(fm: dict):
    """(YYYY-MM-DD or "", unreadable). An unreadable value is never overwritten."""
    raw = fm.get("last_touch")
    if raw in (None, ""):
        return "", False
    try:
        return frontmatter_date(raw).isoformat(), False
    except ValueError:
        return "", True


# ============================================================
# Tally
# ============================================================

def tally(mails, index, selves):
    per = defaultdict(lambda: {"in": 0, "out": 0, "last_in": "", "last_out": "", "subj_in": "", "subj_out": ""})
    unknown = defaultdict(lambda: {"in": 0, "out": 0, "names": set(), "last": "", "subj": ""})
    for m in mails:
        if m["dir"] == "in":
            sender = (m.get("from") or "").lower()
            if not sender or sender in selves or not any(t in selves for t in m.get("to", [])):
                continue  # CC-only, or our own mail filed in a received folder
            pairs = [(sender, m.get("from_name", ""))]
        else:
            names = m.get("to_names") or {}
            pairs = [(t, names.get(t, "")) for t in m.get("to", []) if t and t not in selves]
        subj = " ".join((m.get("subject") or "").replace("|", "/").split())
        for addr, name in pairs:
            slug = index.get(addr)
            if slug:
                p, k = per[slug], m["dir"]
                p[k] += 1
                if m["day"] >= p["last_" + k]:
                    p["last_" + k], p["subj_" + k] = m["day"], subj
            elif not BULK.search(addr):
                u = unknown[addr]
                u[m["dir"]] += 1
                if name and name.lower() != addr:
                    u["names"].add(name)
                if m["day"] >= u["last"]:
                    u["last"], u["subj"] = m["day"], subj
    return per, unknown


def summary_line(p, today, since, until):
    last = max(p["last_in"], p["last_out"])
    subj = p["subj_in"] if p["last_in"] == last else p["subj_out"]
    line = f'- {today} | Email | {TAG} {since}..{until}: {p["out"]} out, {p["in"]} in; last {last} "{subj[:90]}".'
    return line, last


def already_logged(text, last):
    return any(TAG in ln and f'last {last} "' in ln for ln in text.splitlines() if ln.startswith("- "))


def insert_log(text, line):
    if "## Interaction Log" in text:
        return re.sub(r"(## Interaction Log[^\n]*\n)", lambda mm: mm.group(1) + line + "\n", text, count=1)
    return text.rstrip("\n") + "\n\n## Interaction Log\n" + line + "\n"


# ============================================================
# Plan
# ============================================================

def plan(mails, approved, selves, today):
    cards, index, conflicts = load_cards()
    owner_map = C.parse_owner_map(C.crm_config_path())
    errors, aliases, new = [], {}, []

    for addr, slug in (approved.get("aliases") or {}).items():
        addr = addr.strip().lower()
        # entity_path is set from entity_ref before the file is known to exist,
        # so the file itself is checked: apply writes to it after other writes.
        if slug not in cards or not cards[slug]["entity_path"] or not cards[slug]["entity_path"].is_file():
            errors.append(f"alias {addr}: no card with an address-book entity named {slug}")
        elif addr in index and index[addr] != slug:
            errors.append(f"alias {addr}: already belongs to {index[addr]}")
        elif index.get(addr) != slug:
            aliases[addr] = slug
            index[addr] = slug
    ab = C.address_book_home()
    for e in approved.get("new") or []:
        slug, addr = e.get("slug", ""), (e.get("email") or "").strip().lower()
        owner = C.owner_for_type(e.get("type", ""), owner_map) or e.get("owner", "")
        if slug and index.get(addr) == slug:
            continue  # created by an earlier run of this same list
        if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", slug or ""):
            errors.append(f"new {slug!r}: not a kebab-case slug")
        elif slug in cards or (C.contacts_home() / f"{slug}.md").exists() or (ab / f"{slug}.md").exists():
            errors.append(f"new {slug}: a card or entity with that slug exists")
        elif not addr or addr in index:
            errors.append(f"new {slug}: address {addr!r} is empty or already in the CRM")
        elif not (e.get("name") and e.get("type")):
            errors.append(f"new {slug}: name and type are required")
        elif not owner:
            errors.append(f"new {slug}: type {e.get('type')!r} has no owner row in crm-config; give \"owner\"")
        else:
            new.append(dict(e, slug=slug, email=addr, owner=owner))
            index[addr] = slug

    per, unknown = tally(mails, index, selves)
    days = sorted(m["day"] for m in mails)
    since, until = (days[0], days[-1]) if days else (today, today)
    new_slugs = {e["slug"] for e in new}
    for e in new:
        if e["slug"] not in per:
            errors.append(f"new {e['slug']}: no direct correspondence in this export")
    new = [e for e in new if e["slug"] in per]

    updates, unreadable = [], []
    for slug, p in sorted(per.items()):
        if slug in new_slugs:
            continue
        c = cards[slug]
        lt, bad = stored_last_touch(c["fm"])
        line, last = summary_line(p, today, since, until)
        awaiting = bool(p["last_in"]) and p["last_in"] > p["last_out"] and p["last_in"] >= lt
        bump = last > lt
        if bad and (bump or awaiting):
            unreadable.append(slug)
            continue
        if bump or awaiting:
            if awaiting:
                line += f' AWAITING REPLY: inbound {p["last_in"]} "{p["subj_in"][:80]}", no outbound after it.'
            text = c["path"].read_text(encoding="utf-8")
            updates.append({"slug": slug, "path": c["path"], "old": lt, "new": last if bump else lt,
                            "bump": bump, "awaiting": awaiting, "line": line, "p": p,
                            "logged": already_logged(text, last)})
    for e in new:
        p = per[e["slug"]]
        line, last = summary_line(p, today, since, until)
        e["awaiting"] = bool(p["last_in"]) and p["last_in"] > p["last_out"]
        if e["awaiting"]:
            line += f' AWAITING REPLY: inbound {p["last_in"]} "{p["subj_in"][:80]}", no outbound after it.'
        e.update(line=line, last=last, p=p)

    by_name = defaultdict(set)
    for slug, c in cards.items():
        n = _norm_name(c["name"])
        if len(n.split()) >= 2:
            by_name[n].add(slug)
    alias_cands, missing = [], {"external": [], "corporate": []}
    corp = corporate_email_domain()
    for addr, u in unknown.items():
        hits = sorted({s for nm in u["names"] for s in by_name.get(_norm_name(nm), ())})
        if hits:
            alias_cands.append({"email": addr, "slugs": hits, "in": u["in"], "out": u["out"], "last": u["last"]})
        elif u["out"] >= 1 and u["in"] + u["out"] >= 2:
            side = "corporate" if corp and addr.endswith("@" + corp) else "external"
            missing[side].append({"email": addr, "names": sorted(u["names"]), "in": u["in"], "out": u["out"],
                                  "last": u["last"], "subj": u["subj"]})
    for side in missing.values():
        side.sort(key=lambda x: (-(x["in"] + x["out"]), x["email"]))
    return {"since": since, "until": until, "mails": len(mails), "updates": updates, "unreadable": unreadable,
            "aliases": aliases, "new": new, "errors": errors, "alias_candidates": alias_cands,
            "missing": missing, "conflicts": conflicts, "cards": cards}


# ============================================================
# Output
# ============================================================

def report(r):
    bumps = [u for u in r["updates"] if u["bump"]]
    flags = [u for u in r["updates"] if u["awaiting"]] + [e for e in r["new"] if e["awaiting"]]
    print(f"Mail export: {r['mails']} messages, {r['since']}..{r['until']}")
    print(f"\nlast_touch bumps: {len(bumps)}")
    for u in bumps:
        print(f"  {u['slug']}: {u['old'] or '(none)'} -> {u['new']}")
    print(f"\nAWAITING REPLY: {len(flags)}")
    for u in flags:
        note = "  (already in the log)" if u.get("logged") else ("  (new contact)" if "owner" in u else "")
        print(f"  {u['slug']}: inbound {u['p']['last_in']} \"{u['p']['subj_in'][:70]}\"{note}")
    if r["unreadable"]:
        print(f"\nSKIPPED, last_touch present and unreadable, fix by hand: {', '.join(r['unreadable'])}")
    print(f"\nApproved aliases to add: {len(r['aliases'])}")
    for a, s in r["aliases"].items():
        print(f"  {s} +{a}")
    print(f"\nApproved new contacts: {len(r['new'])}")
    for e in r["new"]:
        print(f"  {e['slug']} ({e['type']}, owner {e['owner']}) last {e['last']}")
    if r["errors"]:
        print(f"\nREFUSED from the approved list: {len(r['errors'])}")
        for x in r["errors"]:
            print(f"  {x}")
    print(f"\nPossible second addresses of existing contacts (same name): {len(r['alias_candidates'])}")
    for a in r["alias_candidates"]:
        print(f"  {a['email']} -> {', '.join(a['slugs'])} ({a['out']} out, {a['in']} in, last {a['last']})")
    for side in ("external", "corporate"):
        rows = r["missing"][side]
        print(f"\nFrequent correspondents missing from the CRM, {side}: {len(rows)}")
        for m in rows:
            nm = "; ".join(m["names"]) or "-"
            print(f"  {m['email']} [{nm}] {m['out']} out, {m['in']} in, last {m['last']} \"{m['subj'][:60]}\"")
    if r["conflicts"]:
        print(f"\nAddresses on more than one card, not matched: {len(r['conflicts'])}")
        for a, s in sorted(r["conflicts"].items()):
            print(f"  {a}: {', '.join(s)}")


def apply(r, today):
    written = []
    for u in r["updates"]:
        text = u["path"].read_text(encoding="utf-8")
        new_text = bump_last_touch_in_text(text, u["new"]) if u["bump"] else text
        if not u["logged"]:
            new_text = insert_log(new_text, u["line"])
        if new_text != text:
            atomic_write(u["path"], new_text)
            written.append(u["path"])
    ab = C.address_book_home()
    for addr, slug in r["aliases"].items():
        path = r["cards"][slug]["entity_path"]
        text = path.read_text(encoding="utf-8")
        fm, _ = parse_frontmatter(text)
        oe = frontmatter_list(fm.get("other_emails"))
        if addr not in [x.lower() for x in oe]:
            atomic_write(path, set_frontmatter_field(text, "other_emails", json.dumps(oe + [addr])))
            written.append(path)
    me = operator_slug()
    for e in r["new"]:
        q = lambda s: json.dumps(str(s or ""), ensure_ascii=False)  # noqa: E731
        ep, cp = ab / f"{e['slug']}.md", C.contacts_home() / f"{e['slug']}.md"
        subj = e["p"]["subj_in"] if e["p"]["last_in"] == e["last"] else e["p"]["subj_out"]
        atomic_write(ep, (
            f"---\nslug: {e['slug']}\nname: {q(e['name'])}\ncanonical_email: {q(e['email'])}\nphone: \"\"\n"
            f"linkedin: \"\"\ntelegram: \"\"\nemployer: {q(e.get('employer'))}\ntitle: {q(e.get('title'))}\n"
            f"region: {q(e.get('region'))}\ntimezone: {q(e.get('timezone'))}\noperating_timezone: \"\"\n"
            f"canonical_owner: {e['owner']}\ncreated: {today}\nlast_updated: {today}\n---\n\n"
            f"# {e['name']} - {e.get('employer') or 'Unknown'}\n\n## Profile\n"
            f"- **Relationship:** Found in the {TAG} of {today} ({r['since']}..{r['until']}).\n"
            f"- **Key context:** Last thread: \"{subj[:90]}\".\n"))
        atomic_write(cp, (
            f"---\nname: {q(e['name'])}\nemail: {q(e['email'])}\nentity_ref: {e['slug']}\n"
            f"relationship_type: {e['type']}\nlast_touch: {e['last']}\ncreated: {today}\n"
            f"source: {q(TAG + ' ' + today)}\nstatus: active\ntags: []\n" + (f"owner: {me}\n" if me else "") +
            f"---\n\n# {e['name']}" + (f" ({me})" if me else "") + f"\n\n## Active Commitments\n\n## Interaction Log\n{e['line']}\n"))
        written += [ep, cp]
    if r["new"]:
        written.append(add_people_rows([e["slug"] for e in r["new"]]))
    return [w for w in written if w]


def add_people_rows(slugs):
    """Append a radar row per new contact to the `people` index, health from `scan_contacts`."""
    contacts, *_ = C.scan_contacts(C.parse_config(C.crm_config_path()))
    by_file = {Path(str(c["file"])).name: c for c in contacts}
    pf = C.people_path()
    lines = pf.read_text(encoding="utf-8").split("\n")
    hdr = next((i for i, ln in enumerate(lines) if ln.startswith("| Name | Company | Type | Last Touch")), None)
    if hdr is None:
        print("crm-backfill: no radar table in the people index; rows not added", file=sys.stderr)
        return None
    end = hdr
    while end + 1 < len(lines) and lines[end + 1].startswith("|"):
        end += 1
    rows = []
    for slug in slugs:
        c = by_file.get(f"{slug}.md")
        if c and not any(f"(../{slug}.md)" in ln for ln in lines):
            rows.append(f"| {c['name']} | {c['company']} | {c['type']} | {c['last_touch']} | "
                        f"{str(c['health']).upper()} | [crm](../{slug}.md) |")
    if not rows:
        return None
    lines[end + 1:end + 1] = rows
    atomic_write(pf, "\n".join(lines))
    return pf


def main():
    ap = argparse.ArgumentParser(description="Reconcile CRM cards with a mail metadata export (report by default).")
    ap.add_argument("--mail", type=Path, required=True, help="JSON lines from sync-exchange.py --export-metadata")
    ap.add_argument("--approved", type=Path, help="JSON with the operator-approved aliases and new contacts")
    ap.add_argument("--self", action="append", default=[], metavar="ADDR",
                    help="another address of the operator (EXCHANGE_EMAIL and the operator email are always in)")
    ap.add_argument("--apply", action="store_true", help="write the reported changes")
    args = ap.parse_args()

    selves = self_addresses(args.self)
    if not selves:
        ap.error("no operator address known: set EXCHANGE_EMAIL or pass --self")
    approved = json.loads(args.approved.read_text(encoding="utf-8")) if args.approved else {}
    today = datetime.now(get_default_tz()).date().isoformat()
    r = plan(load_mail(args.mail), approved, selves, today)
    report(r)
    if not args.apply:
        print("\nReport only. Nothing written; --apply writes the bumps, the log lines and the approved entries.")
        return 0
    if r["errors"]:
        print("\nRefusing --apply: fix or drop the refused approved entries first.", file=sys.stderr)
        return 1
    if data_is_readonly():
        print("\nRefusing --apply: this host's data root is read-only.", file=sys.stderr)
        return 1
    written = apply(r, today)
    print(f"\nWritten: {len(written)} file(s).")
    for w in written:
        print(f"  {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
