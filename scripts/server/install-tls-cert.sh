#!/usr/bin/env bash
# Install a TLS certificate delivered over SSH, on the SERVER, as a forced command.
#
# Usage (as root, normally through sudo from an authorized_keys forced command):
#   install-tls-cert.sh <cert-dir> <server-name> status
#   install-tls-cert.sh <cert-dir> <server-name> install   < chain-and-key.pem
#
# The laptop side is `scripts/porkbun.py ssl push`, which keeps the DNS API key
# on the laptop and hands this script only the certificate chain and its key. So
# the server never holds a credential that can edit the zone.
#
# Wiring on the server, one time (names are the operator's, never this file's):
#   install -m 0755 install-tls-cert.sh /usr/local/sbin/install-tls-cert
#   adduser --system --group --shell /bin/sh --home /var/lib/certpush certpush
#   /var/lib/certpush/.ssh/authorized_keys, one line:
#     restrict,command="sudo -n /usr/local/sbin/install-tls-cert /etc/ssl/<name> <name> \"$SSH_ORIGINAL_COMMAND\"" ssh-ed25519 AAAA... cert-push
#   /etc/sudoers.d/certpush (0440):
#     certpush ALL=(root) NOPASSWD: /usr/local/sbin/install-tls-cert
#
# The account needs a real shell because sshd runs a forced command through it;
# `restrict` still denies forwarding, a PTY and agent use. The mode word the
# client sent arrives as $3 and is checked against an allowlist below.
#
# install: reads at most 64 KiB of PEM from stdin, splits certificates from the
# single private key, and refuses unless the key matches the leaf, the leaf
# covers <server-name>, it is inside its validity window and the chain verifies
# against the system trust store. It then swaps fullchain.pem and key.pem by
# rename, runs `nginx -t`, and reloads nginx only if that passes; on a failed
# test it puts the previous files back and exits 1.
#
# status: prints `fingerprint=<SHA-256>` and `not_after=<ISO 8601 UTC>` of the
# installed leaf, or nothing when no certificate is installed.

set -euo pipefail
umask 077

die() { echo "install-tls-cert: $*" >&2; exit 1; }

[[ $# -eq 3 ]] || die "usage: install-tls-cert.sh <cert-dir> <server-name> status|install"
CERT_DIR="$1"; NAME="$2"; MODE="$3"

case "$CERT_DIR" in
    /etc/ssl/?*) ;;
    *) die "cert dir must be under /etc/ssl/: $CERT_DIR" ;;
esac
[[ "$CERT_DIR" != *..* ]] || die "cert dir must not contain '..'"
[[ "$NAME" =~ ^[A-Za-z0-9.-]+$ ]] || die "bad server name"

CHAIN="$CERT_DIR/fullchain.pem"
KEY="$CERT_DIR/key.pem"

status() {
    [[ -f "$CHAIN" ]] || return 0
    local fp end
    fp="$(openssl x509 -in "$CHAIN" -noout -fingerprint -sha256 | cut -d= -f2)"
    end="$(openssl x509 -in "$CHAIN" -noout -enddate | cut -d= -f2)"
    echo "fingerprint=$fp"
    echo "not_after=$(date -u -d "$end" +%Y-%m-%dT%H:%M:%S+00:00)"
}

install_cert() {
    install -d -m 0755 "$CERT_DIR"
    local incoming
    incoming="$(mktemp -d "$CERT_DIR/.incoming.XXXXXX")"
    # Expanded NOW: the EXIT trap fires after this function returns, when the local
    # `incoming` is gone, and under `set -u` a late expansion made every successful install
    # exit 1 (MEASURED 2026-09-29 on the first real push: files swapped, nginx reloaded,
    # then "work: unbound variable", under the name this variable had then).
    trap "rm -rf -- '$incoming'" EXIT

    head -c 65536 > "$incoming/in.pem"
    awk -v c="$incoming/chain.pem" -v k="$incoming/key.pem" '
        /-----BEGIN .*PRIVATE KEY-----/ { out = k; keys++ }
        /-----BEGIN CERTIFICATE-----/   { out = c }
        out != "" { print > out }
        /-----END/ { out = "" }
        END { if (keys != 1) exit 3 }
    ' "$incoming/in.pem" || die "expected exactly one private key in the input"
    [[ -s "$incoming/chain.pem" ]] || die "no certificate in the input"

    # The leaf is the first certificate; the rest are intermediates.
    openssl x509 -in "$incoming/chain.pem" -out "$incoming/leaf.pem"
    diff -q <(openssl x509 -in "$incoming/leaf.pem" -noout -pubkey) \
            <(openssl pkey -in "$incoming/key.pem" -pubout) >/dev/null \
        || die "private key does not match the certificate"
    openssl x509 -in "$incoming/leaf.pem" -noout -checkhost "$NAME" | grep -q "does match" \
        || die "certificate does not cover $NAME"
    openssl x509 -in "$incoming/leaf.pem" -noout -checkend 0 >/dev/null \
        || die "certificate has expired"
    [[ "$(date -u +%s)" -ge "$(date -u -d "$(openssl x509 -in "$incoming/leaf.pem" -noout -startdate | cut -d= -f2)" +%s)" ]] \
        || die "certificate is not valid yet"
    openssl verify -untrusted "$incoming/chain.pem" "$incoming/leaf.pem" >/dev/null \
        || die "chain does not verify against the system trust store"

    chmod 0644 "$incoming/chain.pem"
    chown root:www-data "$incoming/key.pem"; chmod 0640 "$incoming/key.pem"

    [[ -f "$CHAIN" ]] && cp -p "$CHAIN" "$CHAIN.prev"
    [[ -f "$KEY" ]] && cp -p "$KEY" "$KEY.prev"
    mv -f "$incoming/chain.pem" "$CHAIN"
    mv -f "$incoming/key.pem" "$KEY"

    if ! nginx -t >/dev/null 2>&1; then
        if [[ -f "$CHAIN.prev" && -f "$KEY.prev" ]]; then
            mv -f "$CHAIN.prev" "$CHAIN"; mv -f "$KEY.prev" "$KEY"
        fi
        die "nginx -t failed with the new certificate; previous files restored"
    fi
    systemctl reload nginx
    echo "installed $(openssl x509 -in "$CHAIN" -noout -fingerprint -sha256 | cut -d= -f2) for $NAME"
}

case "$MODE" in
    status)  status ;;
    install) install_cert ;;
    *)       die "unknown mode: $MODE (status|install)" ;;
esac
