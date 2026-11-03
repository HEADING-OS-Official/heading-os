#!/usr/bin/env python3
"""test-vm.py -- drive the Proxmox VM that HEADING OS install tests run on.

One VM on a Proxmox host, reached over SSH with the host as jump. Every value
specific to the machine (host alias, VM id, address, user, snapshot name, the
.env key holding the password, ISO, sizes) comes from `test-vm.yaml` under the
registry name `data-config`; the engine ships the empty structure in
scripts/test-vm.example.yaml.

Usage:
  python scripts/test-vm.py reset [--snapshot NAME] [--dry-run]
      Roll the VM back to the clean snapshot, start it, wait until guest SSH
      answers (at most 5 minutes) and print the elapsed time.
  python scripts/test-vm.py status [--dry-run]
      VM state, snapshot list, whether guest SSH answers, and on the guest the
      state of ssh, rustdesk and qemu-guest-agent plus the RustDesk ID.
  python scripts/test-vm.py ssh [--user U] [--dry-run] [-- command...]
      SSH to the guest through the host. Interactive with no command. The
      command words are quoted with shlex.join, so a pipeline is written as
      `-- bash -c 'ls | wc -l'`. Every word after `--` belongs to the
      command, --dry-run included, so options go before it.
  python scripts/test-vm.py rebuild --yes [--dry-run]
      Reinstall the guest from nothing, unattended (preseeded installer ISO,
      RustDesk, autologin), then retake the clean snapshot. DESTROYS the VM's
      current disk and replaces the clean snapshot.
  python scripts/test-vm.py install --source REPO --ref REF --answers FILE --yes [--dry-run]
      Install HEADING OS on the guest unattended (HOS-312): as root the two
      packages install.sh would ask the computer password for, then as the user
      the engine from a git bundle of REPO at REF (an Official tree, install.sh at
      its root), install.sh with the answers FILE (mode 600, the format of
      heading-install.py --answers), and doctor. Exit 0 only when the last
      HEADING-INSTALL line says done and doctor exits 0. --dry-run checks REF,
      install.sh at REF and the answers file locally, and builds no bundle.

--dry-run prints every command, local and remote, instead of running it; it
reads no password and touches neither the host nor the guest.

Password handling: the password is loaded with load_api_key(<password_env>)
and reaches ssh only through the environment of the ssh child, read by an
SSH_ASKPASS helper whose text holds no secret. It never appears in argv, in a
file or in output. The preseed carries only its SHA-512 crypt hash, computed by
`openssl passwd -6` with the password on stdin; the seed, the post-install
script and the remastered ISO exist only on the host and are deleted in a
`finally` once the install is over. One exposure is RustDesk's own: on the
guest, `rustdesk --password` takes the password as an argument for the
moment it runs.

The procedure is the runbook machines/2026-11-01_heading-test-mint-vm.md under
the registry name `system-runbooks`.
"""
import argparse
import base64
import contextlib
import json
import ipaddress
import os
import re
import secrets
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, fields
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import yaml  # noqa: E402

from scripts.utils.api import load_api_key  # noqa: E402
from scripts.utils.colors import BOLD, CYAN, GRAY, GREEN, RED, RESET, YELLOW  # noqa: E402
from scripts.utils.data_path import data_path, data_write_dir  # noqa: E402
from scripts.utils.workspace import get_data_config_dir  # noqa: E402

# ============================================================
# Configuration
# ============================================================

CONFIG_FILE = "test-vm.yaml"
EXAMPLE_FILE = Path(__file__).resolve().parent / "test-vm.example.yaml"

RESET_TIMEOUT_S = 300      # the brief's bound for `reset`
INSTALL_TIMEOUT_S = 1800   # the install measured 6 min on 2026-11-01; 30 min is the ceiling
REBOOT_DOWN_TIMEOUT_S = 120
PROBE_INTERVAL_S = 5
NEW_SYSTEM_TIMEOUT_S = 180
# Written by the post script with a token fresh per rebuild: SSH answering is
# not enough, the system answering must be the one this rebuild installed.
BUILD_MARKER = "/etc/heading-test-vm.build"

SEED_NAME = "ht.seed"
POST_NAME = "ht-post.sh"
ASKPASS_ENV = "TEST_VM_ASKPASS_PW"
# The helper's text: it prints a variable of its own environment, so the file
# holds no secret.
ASKPASS_BODY = f'#!/bin/sh\nprintf \'%s\\n\' "${ASKPASS_ENV}"\n'

# May be empty: rebuild then picks the storage itself.
OPTIONAL_FIELDS = frozenset({"disk_storage"})

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_ENV = re.compile(r"^[A-Z_][A-Z0-9_]*\Z")
_ABS = re.compile(r"^/[A-Za-z0-9._/-]+\Z")
_TZ = re.compile(r"^[A-Za-z_]+(/[A-Za-z0-9_+-]+)*\Z")
_VERSION = re.compile(r"^[0-9]+(\.[0-9]+)+\Z")
_TEXT = re.compile(r"^[^\x00-\x1f'\"\\$`]+\Z")


@dataclass(frozen=True)
class Config:
    host: str
    vmid: int
    name: str
    cores: int
    memory_mb: int
    disk_gb: int
    disk_storage: str
    iso_storage: str
    iso_dir: str
    iso: str
    bridge: str
    address: str
    prefix: int
    gateway: str
    dns: str
    hostname: str
    timezone: str
    user: str
    fullname: str
    snapshot: str
    password_env: str
    rustdesk_version: str


def _check(key: str, value):
    """Refuse a value that could not be passed safely into a remote command."""
    ints = {"vmid": (100, 999999999), "cores": (1, 1024), "memory_mb": (256, 1 << 24),
            "disk_gb": (1, 1 << 16), "prefix": (1, 32)}
    if key in ints:
        lo, hi = ints[key]
        if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
            raise ValueError(f"{key}: an integer from {lo} to {hi} is required")
        return value
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key}: a non-empty string is required")
    if key in ("address", "gateway", "dns"):
        ipaddress.IPv4Address(value)
    elif key == "iso_dir":
        if not _ABS.match(value) or ".." in value.split("/"):
            raise ValueError(f"{key}: an absolute path without '..' is required")
    elif key == "password_env":
        if not _ENV.match(value):
            raise ValueError(f"{key}: an environment variable name is required")
    elif key == "timezone":
        if not _TZ.match(value):
            raise ValueError(f"{key}: an IANA zone name is required")
    elif key == "rustdesk_version":
        if not _VERSION.match(value):
            raise ValueError(f"{key}: a dotted version such as 1.2.3 is required")
    elif key == "fullname":
        if not _TEXT.match(value):
            raise ValueError(f"{key}: plain text without quotes, '$', '\\' or control characters")
    elif key == "iso":
        if not _NAME.match(value) or not value.endswith(".iso"):
            raise ValueError(f"{key}: a file name ending in .iso is required")
    elif not _NAME.match(value):
        raise ValueError(f"{key}: letters, digits, '.', '_' and '-' only")
    return value


def load_config() -> Config:
    path = get_data_config_dir() / CONFIG_FILE
    if not path.exists():
        raise SystemExit(
            f"{RED}No {CONFIG_FILE} under data-config ({path}).{RESET} Copy "
            f"{EXAMPLE_FILE} there and fill in every value.")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise SystemExit(f"{RED}{path}: a mapping is required{RESET}")
    values, problems = {}, []
    for f in fields(Config):
        try:
            if f.name in OPTIONAL_FIELDS and raw.get(f.name) in (None, ""):
                values[f.name] = ""
            else:
                values[f.name] = _check(f.name, raw.get(f.name))
        except ValueError as exc:
            problems.append(str(exc))
    if not problems and remastered_iso_name(values["name"]) == values["iso"]:
        problems.append(f"iso: {values['iso']} is the name rebuild gives the remastered "
                        f"ISO; rename the source ISO or change name")
    if problems:
        raise SystemExit(f"{RED}{path}:{RESET}\n  " + "\n  ".join(problems))
    return Config(**values)


def remastered_iso_name(name: str) -> str:
    """The ISO rebuild writes beside the source and deletes afterwards."""
    return f"{name}-auto.iso"


# ============================================================
# Running commands on the host and the guest
# ============================================================


class TestVmError(RuntimeError):
    """A step failed; the message says which and carries no secret."""


def _tail(text: str, lines: int = 15) -> str:
    return "\n".join((text or "").strip().splitlines()[-lines:])


class Runner:
    def __init__(self, cfg: Config, dry_run: bool):
        self.cfg = cfg
        self.dry_run = dry_run
        self._password = None

    # --- primitives -------------------------------------------------------

    def _show(self, where: str, argv: list[str], stdin_note: str | None):
        print(f"{GRAY}[dry-run] {where}:{RESET} {shlex.join(argv)}")
        if stdin_note:
            print(f"{GRAY}          stdin: {stdin_note}{RESET}")

    def _run(self, where, argv, *, data=None, stdin_note=None, check=True,
             capture=True, env=None, timeout=600, what=""):
        if self.dry_run:
            self._show(where, argv, stdin_note)
            return subprocess.CompletedProcess(argv, 0, "", "")
        proc = subprocess.run(argv, input=data, capture_output=capture, env=env,
                              timeout=timeout, text=True)
        if check and proc.returncode != 0:
            raise TestVmError(f"{what or argv[0]} failed (exit {proc.returncode}):\n"
                              f"{_tail(proc.stderr or proc.stdout or '')}")
        return proc

    def local(self, argv, **kw):
        return self._run("local", argv, **kw)

    def host(self, argv: list[str], **kw):
        """Run argv on the Proxmox host, quoted as one remote command word list."""
        remote = shlex.join(str(a) for a in argv)
        kw.setdefault("what", f"host: {remote[:80]}")
        return self._run("host", ["ssh", "-o", "BatchMode=yes", self.cfg.host, remote], **kw)

    def password(self) -> str:
        if self._password is None:
            self._password = load_api_key(self.cfg.password_env)
        return self._password

    def known_hosts(self) -> Path:
        base = (data_path("runtime:scratch/state") if self.dry_run
                else data_write_dir("runtime:scratch/state"))
        return base / "test-vm-known_hosts"

    def _guest_argv(self, user: str, remote: str | None, tty: bool) -> list[str]:
        argv = ["ssh", "-J", self.cfg.host,
                "-o", "PreferredAuthentications=password",
                "-o", "PubkeyAuthentication=no",
                "-o", "NumberOfPasswordPrompts=1",
                "-o", "ConnectTimeout=15",
                "-o", "StrictHostKeyChecking=accept-new",
                "-o", f"UserKnownHostsFile={self.known_hosts()}"]
        if tty:
            argv.append("-t")
        argv.append(f"{user}@{self.cfg.address}")
        if remote is not None:
            argv.append(remote)
        return argv

    @contextlib.contextmanager
    def _askpass_env(self):
        """The ssh child's environment: the helper's path and the password, nothing on disk."""
        if self.dry_run:
            print(f"{GRAY}[dry-run] env: SSH_ASKPASS=<helper printing ${ASKPASS_ENV}> "
                  f"SSH_ASKPASS_REQUIRE=force {ASKPASS_ENV}=<load_api_key("
                  f"{self.cfg.password_env!r})>{RESET}")
            yield None
            return
        password = self.password()
        fd, helper = tempfile.mkstemp(prefix="test-vm-askpass.", suffix=".sh",
                                      dir=data_write_dir("runtime:scratch/tmp"))
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(ASKPASS_BODY)
            os.chmod(helper, 0o700)
            # Built from os.environ so X31C_TRACE_ID and the rest are inherited.
            yield dict(os.environ, SSH_ASKPASS=helper, SSH_ASKPASS_REQUIRE="force",
                       DISPLAY=os.environ.get("DISPLAY", ":0"), **{ASKPASS_ENV: password})
        finally:
            Path(helper).unlink(missing_ok=True)

    def guest(self, user: str, argv: list[str] | None, *, tty=False, **kw):
        """Run argv on the guest as user, or open an interactive session when argv is None."""
        remote = None if argv is None else shlex.join(str(a) for a in argv)
        kw.setdefault("what", f"guest {user}: {(remote or 'interactive')[:80]}")
        with self._askpass_env() as env:
            return self._run("guest", self._guest_argv(user, remote, tty), env=env, **kw)

    # --- probes -----------------------------------------------------------

    def ssh_answers(self) -> bool:
        """TCP 22 on the guest, probed from the host as the runbook does."""
        probe = ["timeout", "3", "bash", "-c", f"</dev/tcp/{self.cfg.address}/22"]
        return self.host(probe, check=False, timeout=30).returncode == 0

    def wait_build(self, build_id: str, timeout_s: int):
        """Wait until the guest answering SSH carries this rebuild's marker."""
        argv = ["cat", "--", BUILD_MARKER]
        if self.dry_run:
            self.guest("root", argv, check=False)
            print(f"{GRAY}[dry-run] repeat until it prints this rebuild's token, at most "
                  f"{timeout_s} s{RESET}")
            return
        start = time.monotonic()
        while time.monotonic() - start < timeout_s:
            got = self.guest("root", argv, check=False, timeout=60)
            if got.returncode == 0 and got.stdout.strip() == build_id:
                return
            time.sleep(PROBE_INTERVAL_S)
        raise TestVmError(f"SSH on {self.cfg.address} answers, but not from the system this "
                          f"rebuild installed ({BUILD_MARKER} lacks its token)")

    def wait_ssh(self, timeout_s: int, *, up=True) -> float:
        start = time.monotonic()
        state = "answers" if up else "stops answering"
        if self.dry_run:
            self.ssh_answers()
            print(f"{GRAY}[dry-run] repeat the probe every {PROBE_INTERVAL_S} s until SSH "
                  f"{state}, at most {timeout_s} s{RESET}")
            return 0.0
        while time.monotonic() - start < timeout_s:
            if self.ssh_answers() == up:
                return time.monotonic() - start
            time.sleep(PROBE_INTERVAL_S)
        raise TestVmError(f"SSH on {self.cfg.address} did not reach '{state}' "
                          f"within {timeout_s} s")


# ============================================================
# What runs on the guest
# ============================================================

GUEST_CHECK = r"""
rc=0
for s in ssh rustdesk qemu-guest-agent; do
  st=$(systemctl is-active "$s" 2>/dev/null); echo "$s: ${st:-unknown}"
  [ "$st" = active ] || rc=1
done
echo "rustdesk id: $(rustdesk --get-id 2>/dev/null || echo unknown)"
if [ "$1" = net ]; then
  if curl -fsS -m 15 -o /dev/null https://github.com; then echo "internet: ok"; else echo "internet: FAIL"; rc=1; fi
fi
exit $rc
"""

# Reads the password as the first line of stdin and the script itself after it.
STDIN_SCRIPT_WRAPPER = ('IFS= read -r PW; f=$(mktemp); trap \'rm -f "$f"\' EXIT; cat > "$f"; '
                        'printf \'%s\\n\' "$PW" | bash "$f" "$@"')

# RustDesk with its permanent password in the root, lightdm and user configs
# (on the first Mint VM a mismatch between root and lightdm gave "Wrong
# password"), and lightdm autologin of the user so a session exists.
GUEST_FINISH = r"""
set -e
IFS= read -r PW
VER="$1"; U="$2"
export DEBIAN_FRONTEND=noninteractive
deb="rustdesk-$VER-x86_64.deb"
# A root-owned private directory: a fixed /tmp name lets a guest user plant a symlink.
d=$(mktemp -d)
cd "$d"
trap 'rm -rf "$d"' EXIT
curl -fsSL -o "$deb" "https://github.com/rustdesk/rustdesk/releases/download/$VER/$deb"
apt-get -qq update >/dev/null
apt-get -qq install -y "./$deb" >/dev/null
rm -f "$deb"
systemctl enable --now rustdesk >/dev/null 2>&1; sleep 8
rustdesk --password "$PW" >/dev/null 2>&1 || true
unset PW
sleep 3
mkdir -p /etc/lightdm/lightdm.conf.d
printf '[Seat:*]\nautologin-user=%s\nautologin-user-timeout=0\n' "$U" > /etc/lightdm/lightdm.conf.d/70-autologin.conf
systemctl restart lightdm; sleep 15
systemctl restart rustdesk; sleep 12
python3 - "$U" <<'PY'
import os, pwd, re, sys, tomllib
user = sys.argv[1]
root = "/root/.config/rustdesk/RustDesk.toml"
targets = {"lightdm": "/var/lib/lightdm/.config/rustdesk/RustDesk.toml",
           user: os.path.join(pwd.getpwnam(user).pw_dir, ".config/rustdesk/RustDesk.toml")}
text = open(root).read()
keep = {k: next((l for l in text.splitlines() if l.startswith(k + " = ")), None) for k in ("password", "salt")}
if not all(keep.values()):
    sys.exit("root RustDesk config holds no password and salt")
ref = tomllib.loads(text)
ok = True
for owner, path in targets.items():
    created = []
    d = os.path.dirname(path)
    while not os.path.isdir(d):
        created.append(d); d = os.path.dirname(d)
    for d in reversed(created):
        os.mkdir(d, 0o700)
    new = not os.path.exists(path)
    t = "" if new else open(path).read()
    for k, line in keep.items():
        t = line + "\n" + re.sub(rf"(?m)^{k} = .*\n?", "", t)
    with open(path, "w") as fh:
        fh.write(t)
    if new:
        pw = pwd.getpwnam(owner)
        for p in created + [path]:
            os.chown(p, pw.pw_uid, pw.pw_gid)
    got = tomllib.load(open(path, "rb"))
    same = got.get("password") == ref.get("password") and got.get("salt") == ref.get("salt")
    print(f"{owner} config: password and salt match root: {same}")
    ok = ok and same
sys.exit(0 if ok else 1)
PY
systemctl restart rustdesk; sleep 8
echo "rustdesk: $(dpkg -s rustdesk | grep ^Version) | service $(systemctl is-active rustdesk) | id $(rustdesk --get-id 2>/dev/null)"
"""


# ============================================================
# The unattended installer: preseed, post script, grub entry
# ============================================================


def build_seed(cfg: Config, hashed: str) -> str:
    return f"""# Preseed written by scripts/test-vm.py rebuild for VM {cfg.vmid} {cfg.name}.
d-i debian-installer/locale string en_US.UTF-8
d-i localechooser/supported-locales multiselect en_US.UTF-8
d-i keyboard-configuration/layoutcode string us
d-i keyboard-configuration/xkb-keymap select us
d-i time/zone string {cfg.timezone}
d-i clock-setup/utc boolean true
d-i netcfg/get_hostname string {cfg.hostname}
d-i netcfg/get_domain string
d-i partman-auto/disk string /dev/sda
d-i partman-auto/method string regular
d-i partman-auto/choose_recipe select atomic
d-i partman-partitioning/confirm_write_new_label boolean true
d-i partman/choose_partition select finish
d-i partman/confirm boolean true
d-i partman/confirm_nooverwrite boolean true
d-i partman/confirm_write_new_label boolean true
d-i partman-efi/non_efi_system boolean true
d-i passwd/user-fullname string {cfg.fullname}
d-i passwd/username string {cfg.user}
d-i passwd/user-password-crypted password {hashed}
d-i user-setup/allow-password-weak boolean true
d-i user-setup/encrypt-home boolean false
d-i passwd/auto-login boolean false
ubiquity ubiquity/use_nonfree boolean false
ubiquity ubiquity/download_updates boolean false
ubiquity ubiquity/summary note
ubiquity ubiquity/reboot boolean true
ubiquity ubiquity/poweroff boolean false
ubiquity ubiquity/success_command string sh /cdrom/preseed/{POST_NAME}
"""


def build_post(cfg: Config, hashed: str, build_id: str) -> str:
    cidr = f"{cfg.address}/{cfg.prefix}"
    root_line = shlex.quote(f"root:{hashed}")
    return f"""#!/bin/sh
# Runs at the end of the unattended install (scripts/test-vm.py rebuild): static network, root password, SSH for root and the user.
exec > /target/var/log/ht-post.log 2>&1
chmod 600 /target/var/log/ht-post.log
set -x
IF=$(ls /sys/class/net | grep -v -E '^(lo|wl)' | head -1)
ip addr add {cidr} dev $IF; ip link set $IF up; ip route add default via {cfg.gateway}
mkdir -p /target/etc/NetworkManager/system-connections
cat > /target/etc/NetworkManager/system-connections/nat.nmconnection <<'NM'
[connection]
id=nat
type=ethernet
autoconnect=true
[ipv4]
method=manual
address1={cidr},{cfg.gateway}
dns={cfg.dns};
[ipv6]
method=disabled
NM
chmod 600 /target/etc/NetworkManager/system-connections/nat.nmconnection
# Not traced: the trace would write the password hash into the log.
set +x
echo {root_line} | chroot /target chpasswd -e
set -x
cp -a /target/etc/resolv.conf /target/etc/resolv.conf.ht 2>/dev/null
rm -f /target/etc/resolv.conf; echo 'nameserver {cfg.dns}' > /target/etc/resolv.conf
mount --bind /dev /target/dev; mount --bind /proc /target/proc; mount --bind /sys /target/sys
chroot /target sh -c 'DEBIAN_FRONTEND=noninteractive apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y openssh-server qemu-guest-agent'
printf '# Test VM: root and the user log in over SSH with a password.\\nPermitRootLogin yes\\nPasswordAuthentication yes\\n' > /target/etc/ssh/sshd_config.d/10-root-password.conf
chroot /target systemctl enable ssh qemu-guest-agent
echo {build_id} > /target{BUILD_MARKER}
umount /target/sys /target/proc /target/dev
rm -f /target/etc/resolv.conf; mv /target/etc/resolv.conf.ht /target/etc/resolv.conf
"""


def patch_grub(text: str, cfg: Config) -> str:
    """Make the first boot entry install unattended from the seed, after a 3 s menu.

    The runbook's measured edit put the seed arguments after the entry's
    `hostname=` argument (which it renamed); without one they go after the
    kernel path. Kernel argument order does not matter to the installer.
    """
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        m = re.match(r"(\s*linux\s+\S+)(.*)$", line.rstrip("\n"))
        if not m:
            continue
        # The ISO's own seed argument would win over ours, so it goes.
        args = [a for a in m.group(2).split()
                if not a.startswith(("file=", "preseed/file="))]
        at = 0
        for j, arg in enumerate(args):
            if arg.startswith("hostname="):
                args[j] = f"hostname={cfg.hostname}"
                at = j + 1
                break
        args[at:at] = [f"file=/cdrom/preseed/{SEED_NAME}", "automatic-ubiquity", "noprompt"]
        lines[i] = m.group(1) + " " + " ".join(args) + "\n"
        return "set timeout=3\nset default=0\n" + "".join(lines)
    raise TestVmError("the ISO's grub.cfg has no 'linux' line to extend")


# ============================================================
# Commands
# ============================================================


def _ok(msg):
    print(f"{GREEN}{msg}{RESET}")


def cmd_reset(cfg: Config, r: Runner, args) -> int:
    snap = _check("snapshot", args.snapshot or cfg.snapshot)
    start = time.monotonic()
    r.host(["qm", "rollback", cfg.vmid, snap])
    r.host(["qm", "start", cfg.vmid])
    r.wait_ssh(RESET_TIMEOUT_S)
    if not r.dry_run:
        _ok(f"VM {cfg.vmid} at snapshot {snap}; SSH on {cfg.address} answers after "
            f"{time.monotonic() - start:.0f} s")
    return 0


def cmd_status(cfg: Config, r: Runner, args) -> int:
    st = r.host(["qm", "status", cfg.vmid], check=False)
    print(f"{BOLD}VM {cfg.vmid} {cfg.name}:{RESET} {(st.stdout or st.stderr).strip()}")
    snaps = r.host(["qm", "listsnapshot", cfg.vmid], check=False)
    print(f"{BOLD}snapshots:{RESET}\n{(snaps.stdout or snaps.stderr).rstrip()}")
    if r.dry_run:
        r.ssh_answers()
        print(f"{GRAY}[dry-run] when SSH answers:{RESET}")
    elif not r.ssh_answers():
        print(f"{BOLD}guest SSH:{RESET} {YELLOW}not answering on {cfg.address}{RESET}")
        return 1
    else:
        print(f"{BOLD}guest SSH:{RESET} answering on {cfg.address}")
    g = r.guest("root", ["bash", "-c", GUEST_CHECK, "test-vm"], check=False, timeout=90)
    if not r.dry_run:
        print(g.stdout.rstrip())
        if g.returncode != 0:
            print(f"{YELLOW}guest check exit {g.returncode}{RESET} {_tail(g.stderr, 3)}")
    return 0 if st.returncode == 0 and g.returncode == 0 else 1


def cmd_ssh(cfg: Config, r: Runner, args) -> int:
    user = _check("user", args.user or cfg.user)
    command = list(args.command)
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        proc = r.guest(user, None, tty=True, capture=False, check=False, timeout=None)
    else:
        proc = r.guest(user, command, capture=False, check=False, timeout=None)
    return proc.returncode


def _password_hash(r: Runner) -> str:
    if r.dry_run:
        print(f"{GRAY}[dry-run] local: openssl passwd -6 -stdin{RESET}")
        print(f"{GRAY}          stdin: <load_api_key({r.cfg.password_env!r})>{RESET}")
        return "<sha512-crypt-hash>"
    proc = subprocess.run(["openssl", "passwd", "-6", "-stdin"], input=r.password() + "\n",
                          capture_output=True, text=True, timeout=30)
    hashed = proc.stdout.strip()
    if proc.returncode != 0 or not hashed.startswith("$6$"):
        raise TestVmError(f"openssl passwd -6 failed (exit {proc.returncode})")
    return hashed


def _snapshot_names(listing: str) -> set[str]:
    names = set()
    for line in listing.splitlines():
        tok = line.replace("`->", " ").replace("`-", " ").split()
        if tok:
            names.add(tok[0])
    return names


def _scsi0_volume(config_text: str) -> str:
    for line in config_text.splitlines():
        if line.startswith("scsi0:"):
            volid = line.split(":", 1)[1].split(",")[0].strip()
            if re.match(r"^[A-Za-z0-9._-]+:[A-Za-z0-9._/-]+$", volid):
                return volid
            raise TestVmError(f"unexpected scsi0 volume {volid!r} in qm config")
    raise TestVmError("qm config shows no scsi0 disk")


@dataclass(frozen=True)
class Storage:
    name: str
    type: str
    active: bool
    avail_kib: int
    on_root: bool = False
    # False when the root-filesystem probe could not answer for it: never chosen.
    usable: bool = True

    def free(self) -> str:
        return f"{self.avail_kib / (1024 * 1024):.0f} GiB free"


# Storage types whose `pvesm path` is a block device wipefs can clear. A file
# image (dir, nfs, cifs, btrfs) holds its partition table inside the file,
# and rbd answers a librbd address, not a device.
BLOCK_STORAGE_TYPES = ("lvm", "lvmthin", "zfspool")
# The storage types that can sit on the host's own root filesystem.
LOCAL_FILE_STORAGE_TYPES = ("dir", "btrfs")


def parse_pvesm_status(text: str) -> list[Storage]:
    """Rows of `pvesm status`: Name Type Status Total Used Available %, sizes in KiB."""
    rows = []
    for line in text.splitlines()[1:]:
        f = line.split()
        if len(f) >= 6 and _NAME.match(f[0]) and f[5].isdigit():
            rows.append(Storage(f[0], f[1], f[2] == "active", int(f[5])))
    return rows


def parse_storage_paths(cfg_text: str) -> dict[str, str]:
    """The `path` of each storage in /etc/pve/storage.cfg."""
    paths, current = {}, None
    for line in cfg_text.splitlines():
        head = re.match(r"^([a-z0-9]+):\s+(\S+)\s*$", line)
        if head:
            current = head.group(2)
            continue
        path = re.match(r"^\s+path\s+(\S+)\s*$", line)
        if path and current and _ABS.match(path.group(1)):
            paths[current] = path.group(1)
    return paths


def _mount_device(findmnt_source: str) -> str:
    """The device of a findmnt SOURCE, without a btrfs subvolume suffix such as `[/@]`."""
    return findmnt_source.strip().split("[", 1)[0]


def image_storages(r: Runner) -> list[Storage]:
    """Every storage that takes VM disks, each marked when it sits on the host's root filesystem.

    Only ACTIVE dir and btrfs storages are probed: an inactive one may have no
    path on disk. A probe that fails drops that one storage, with the reason
    printed, and never the whole rebuild.
    """
    rows = parse_pvesm_status(r.host(["pvesm", "status", "--content", "images"]).stdout)
    local = [s for s in rows if s.active and s.type in LOCAL_FILE_STORAGE_TYPES]
    if not local and not r.dry_run:
        return rows
    paths = parse_storage_paths(r.host(["cat", "/etc/pve/storage.cfg"]).stdout)
    root = _mount_device(r.host(["findmnt", "-n", "-o", "SOURCE", "--target", "/"]).stdout)
    if not root and not r.dry_run:
        # Without the root device no storage can be told off-root, so none is guessed.
        raise TestVmError("findmnt named no device for the host's root filesystem")
    marked = []
    for s in rows:
        on_root, usable = False, True
        if s in local:
            src = None
            if s.name in paths:
                src = r.host(["findmnt", "-n", "-o", "SOURCE", "--target", paths[s.name]],
                             check=False)
            if src is None or src.returncode != 0 or not src.stdout.strip():
                why = ("no path in /etc/pve/storage.cfg" if src is None
                       else f"findmnt on {paths[s.name]} failed: {_tail(src.stderr, 1)}")
                print(f"{YELLOW}storage {s.name} left out: {why}{RESET}", file=sys.stderr)
                usable = False
            else:
                on_root = _mount_device(src.stdout) == root
        marked.append(Storage(s.name, s.type, s.active, s.avail_kib, on_root, usable))
    return marked


def choose_storage(storages: list[Storage], configured: str, override: bool) -> Storage:
    """The storage a NEW VM's disks go on: the active one with the most available space.

    Operator rule, 2026-11-01: every new VM goes on the largest storage
    Proxmox has, never on the one holding the host's root filesystem while
    another storage can take images.
    """
    active = [s for s in storages if s.active and s.usable]
    if not active:
        raise TestVmError("pvesm status lists no active, usable storage that takes VM images")
    off_root = [s for s in active if not s.on_root]
    largest = max(off_root or active, key=lambda s: s.avail_kib)
    if not configured:
        return largest
    chosen = next((s for s in active if s.name == configured), None)
    if chosen is None:
        raise TestVmError(f"disk_storage {configured} is not an active, usable storage that "
                          f"takes VM images on this host")
    if chosen.on_root and off_root:
        raise TestVmError(f"disk_storage {configured} sits on the host's root filesystem and "
                          f"{largest.name} can take the disk; a VM disk never goes on the "
                          f"root filesystem while another storage exists")
    if chosen.name != largest.name and not override:
        raise TestVmError(f"disk_storage is {configured} ({chosen.free()}), but the largest "
                          f"storage is {largest.name} ({largest.free()}); remove disk_storage, "
                          f"set it to {largest.name}, or pass --storage-override")
    return chosen


def check_disk_wipeable(r: Runner, storages: list[Storage]) -> str:
    """The existing VM's scsi0 volume, refused unless its storage is block storage.

    Reads only: it runs before anything stops or changes the VM.
    """
    if r.dry_run:
        r.host(["qm", "config", r.cfg.vmid])
        print(f"{GRAY}[dry-run] refuse unless the scsi0 storage type is one of "
              f"{', '.join(BLOCK_STORAGE_TYPES)}{RESET}")
        return "<scsi0-volume>"
    volid = _scsi0_volume(r.host(["qm", "config", r.cfg.vmid]).stdout)
    name = volid.split(":", 1)[0]
    kind = next((s.type for s in storages if s.name == name), "unknown")
    if kind not in BLOCK_STORAGE_TYPES:
        raise TestVmError(
            f"VM {r.cfg.vmid} has its disk on {name} ({kind}); rebuild wipes an existing "
            f"disk only on {', '.join(BLOCK_STORAGE_TYPES)} storage. Nothing was changed. "
            f"Remove the VM by hand to rebuild it from nothing on the largest storage.")
    return volid


def _wipe_system_disk(r: Runner, volid: str):
    """Erase the partition tables of an existing VM's system disk.

    The boot order is scsi0 first, as measured on the first install: an empty
    disk falls through to the installer ISO, and the installed system boots
    once the installer reboots. An old bootable install would boot instead
    and pass for the new one, so its signatures go first.
    """
    vmid = r.cfg.vmid
    st = r.host(["qm", "status", vmid]).stdout
    if not r.dry_run and "status: stopped" not in st:
        raise TestVmError(f"VM {vmid} is not stopped ({st.strip()}); its disk is not wiped")
    dev = r.host(["pvesm", "path", volid]).stdout.strip() or "<scsi0-device>"
    if not r.dry_run and not re.match(r"^/dev/[A-Za-z0-9._/-]+$", dev):
        raise TestVmError(f"pvesm path answered {dev!r} for {volid}")
    r.host(["wipefs", "-a", "--", dev])


def _create_argv(cfg: Config, storage: str) -> list:
    return ["qm", "create", cfg.vmid, "--name", cfg.name, "--memory", cfg.memory_mb,
            "--cores", cfg.cores, "--cpu", "host", "--machine", "q35", "--bios", "ovmf",
            "--efidisk0", f"{storage}:1,efitype=4m,pre-enrolled-keys=0",
            "--scsihw", "virtio-scsi-single",
            "--scsi0", f"{storage}:{cfg.disk_gb},discard=on,iothread=1",
            "--net0", f"virtio,bridge={cfg.bridge}", "--ostype", "l26", "--vga", "std",
            "--agent", "enabled=1", "--onboot", "0"]


def cmd_rebuild(cfg: Config, r: Runner, args) -> int:
    if not args.yes:
        print(f"{RED}rebuild reinstalls VM {cfg.vmid} ({cfg.name}) from {cfg.iso}: it "
              f"DESTROYS the VM's current disk (its partition tables are wiped) and replaces "
              f"snapshot {cfg.snapshot}.{RESET}\n"
              f"Re-run with --yes to proceed.")
        return 2
    vmid = cfg.vmid
    hashed = _password_hash(r)

    # Every check reads; nothing stops, creates or changes the VM until all pass.
    exists = r.host(["qm", "status", vmid], check=False).returncode == 0
    storages = image_storages(r)
    if r.dry_run:
        print(f"{GRAY}[dry-run] when that qm status fails (no such VM): pick the active "
              f"storage with the most available space, off the root filesystem{RESET}")
        target = cfg.disk_storage or "<largest-storage>"
        print(f"{GRAY}[dry-run] otherwise:{RESET}")
        volid = check_disk_wipeable(r, storages)
    elif exists:
        volid = check_disk_wipeable(r, storages)
    else:
        chosen = choose_storage(storages, cfg.disk_storage, args.storage_override)
        target = chosen.name
        print(f"{CYAN}VM disks go on {chosen.name} ({chosen.type}, {chosen.free()}){RESET}")

    kh = r.known_hosts()
    if r.dry_run or kh.exists():
        # The reinstalled guest has a new host key.
        r.local(["ssh-keygen", "-R", cfg.address, "-f", str(kh)], check=False)
    if r.dry_run:
        print(f"{GRAY}[dry-run] when no such VM:{RESET}")
        r.host(_create_argv(cfg, target))
        print(f"{GRAY}[dry-run] otherwise:{RESET}")
    if r.dry_run or exists:
        r.host(["qm", "stop", vmid], check=False)
        _wipe_system_disk(r, volid)
    else:
        r.host(_create_argv(cfg, target))
        _ok(f"VM {vmid} created on {target}")

    work = r.host(["mktemp", "-d", "-t", "test-vm.XXXXXXXX"]).stdout.strip() or "<host-workdir>"
    if not work.startswith("/") and not r.dry_run:
        raise TestVmError(f"mktemp on the host answered {work!r}")
    iso_in = f"{cfg.iso_dir}/{cfg.iso}"
    out_name = remastered_iso_name(cfg.name)
    out_iso = f"{cfg.iso_dir}/{out_name}"
    build_id = secrets.token_hex(8)
    seed, post = build_seed(cfg, hashed), build_post(cfg, hashed, build_id)
    try:
        r.host(["install", "-m", "600", "/dev/stdin", f"{work}/{SEED_NAME}"], data=seed,
               stdin_note=f"preseed, {len(seed)} chars, carries the password hash")
        r.host(["install", "-m", "600", "/dev/stdin", f"{work}/{POST_NAME}"], data=post,
               stdin_note=f"post-install script, {len(post)} chars, carries the password hash")
        r.host(["xorriso", "-osirrox", "on", "-indev", iso_in,
                "-extract", "/boot/grub/grub.cfg", f"{work}/grub.orig.cfg"])
        orig = r.host(["cat", "--", f"{work}/grub.orig.cfg"]).stdout
        grub = "<patched grub.cfg>" if r.dry_run else patch_grub(orig, cfg)
        r.host(["install", "-m", "600", "/dev/stdin", f"{work}/grub.cfg"], data=grub,
               stdin_note="grub.cfg with the seed arguments on the first entry")
        r.host(["rm", "-f", "--", out_iso])
        r.host(["xorriso", "-indev", iso_in, "-outdev", out_iso,
                "-map", f"{work}/grub.cfg", "/boot/grub/grub.cfg",
                "-map", f"{work}/{SEED_NAME}", f"/preseed/{SEED_NAME}",
                "-map", f"{work}/{POST_NAME}", f"/preseed/{POST_NAME}",
                "-boot_image", "any", "replay"], timeout=900)
        r.host(["qm", "set", vmid, "--ide2", f"{cfg.iso_storage}:iso/{out_name},media=cdrom",
                "--boot", "order=scsi0;ide2"])
        r.host(["qm", "start", vmid])
        print(f"{CYAN}installing; SSH answered after about 6 min on 2026-11-01{RESET}")
        took = r.wait_ssh(INSTALL_TIMEOUT_S)
        r.wait_build(build_id, NEW_SYSTEM_TIMEOUT_S)
        if not r.dry_run:
            _ok(f"installed; SSH answers after {took:.0f} s")
    finally:
        # The seed, the post script and the remastered ISO hold the password hash.
        for argv in (["rm", "-rf", "--", work], ["rm", "-f", "--", out_iso],
                     ["qm", "set", vmid, "--ide2", "none,media=cdrom"]):
            try:
                r.host(argv, timeout=120)
            except (TestVmError, subprocess.TimeoutExpired) as exc:
                print(f"{RED}cleanup step failed, do it by hand on {cfg.host}: "
                      f"{shlex.join(str(a) for a in argv)}\n{exc}{RESET}", file=sys.stderr)

    stdin = None if r.dry_run else r.password() + "\n" + GUEST_FINISH
    r.guest("root", ["bash", "-c", STDIN_SCRIPT_WRAPPER, "test-vm", cfg.rustdesk_version, cfg.user],
            data=stdin, stdin_note="password line, then the RustDesk and autologin script",
            capture=False, timeout=900)

    r.guest("root", ["systemctl", "reboot"], check=False, timeout=60)
    r.wait_ssh(REBOOT_DOWN_TIMEOUT_S, up=False)
    r.wait_ssh(RESET_TIMEOUT_S)
    r.guest("root", ["bash", "-c", GUEST_CHECK, "test-vm", "net"], capture=False, timeout=120,
            what="the post-reboot check (ssh, rustdesk, guest agent, internet)")

    listing = r.host(["qm", "listsnapshot", vmid]).stdout
    if r.dry_run:
        print(f"{GRAY}[dry-run] when {cfg.snapshot} is listed:{RESET}")
    if r.dry_run or cfg.snapshot in _snapshot_names(listing):
        r.host(["qm", "delsnapshot", vmid, cfg.snapshot])
    r.host(["qm", "snapshot", vmid, cfg.snapshot, "--description",
            f"Clean install by scripts/test-vm.py rebuild, {time.strftime('%Y-%m-%d')}: "
            f"SSH, guest agent, RustDesk {cfg.rustdesk_version}, before any HEADING OS install."],
           timeout=300)
    if not r.dry_run:
        _ok(f"snapshot {cfg.snapshot} retaken on VM {vmid}")
    return 0


# ============================================================
# CLI
# ============================================================


# The packages install.sh asks pkexec for (official/overlay/install.sh, the PKGS list).
INSTALL_PACKAGES = "git libatomic1 zstd"
STATUS_PREFIX = "HEADING-INSTALL: "  # scripts/heading-install.py STATUS_PREFIX


def _install_preflight(args) -> str:
    """The local checks of `install`, dry run included; returns the answers file's text."""
    from scripts.utils import install_answers

    source = Path(args.source).expanduser().resolve()
    for argv, what in ((["git", "-C", str(source), "rev-parse", "--verify", "--quiet", args.ref],
                        f"{source} has no ref {args.ref}"),
                       (["git", "-C", str(source), "cat-file", "-e", f"{args.ref}:install.sh"],
                        f"{source} at {args.ref} has no install.sh at its root, so it is not an Official tree")):
        if subprocess.run(argv, capture_output=True, timeout=60).returncode != 0:
            raise TestVmError(what)
    answers = Path(args.answers).expanduser().absolute()
    try:
        filed = install_answers.read_answers_file(answers)
    except install_answers.AnswersFileError as exc:
        raise TestVmError(str(exc)) from exc
    return json.dumps(filed) + "\n"  # the answers as checked, never a second read of the file


def _bundle_text(source: Path, ref: str) -> str:
    """A git bundle of `source` at `ref`, base64, built under the runtime scratch and removed."""
    work = Path(tempfile.mkdtemp(prefix="test-vm-bundle.", dir=data_write_dir("runtime:scratch/tmp")))
    try:
        bundle = work / "heading-os.bundle"
        proc = subprocess.run(["git", "-C", str(source), "bundle", "create", str(bundle), ref],
                              capture_output=True, text=True, timeout=300)
        if proc.returncode != 0:
            raise TestVmError(f"git bundle create failed (exit {proc.returncode}):\n{_tail(proc.stderr)}")
        return base64.b64encode(bundle.read_bytes()).decode()
    finally:
        for f in work.iterdir():
            f.unlink()
        work.rmdir()


def cmd_install(cfg: Config, r: Runner, args) -> int:
    if not args.yes and not r.dry_run:
        print(f"{RED}install runs HEADING OS's installer on VM {cfg.vmid} ({cfg.name}): as root it "
              f"installs {INSTALL_PACKAGES}, and as {cfg.user} it replaces ~/heading-src and installs "
              f"into ~/HeadingOS.{RESET}\n"
              f"Re-run with --yes to proceed.")
        return 2
    answers = _install_preflight(args)
    source = Path(args.source).expanduser().resolve()
    ref = shlex.quote(args.ref)
    user = cfg.user

    r.guest("root", ["bash", "-c", f"apt-get update -qq && apt-get install -y -qq {INSTALL_PACKAGES}"],
            timeout=900, what=f"apt-get install {INSTALL_PACKAGES}")
    r.guest(user, ["bash", "-c", 'install -m 600 /dev/stdin "$HOME/heading-answers.json"'],
            data=None if r.dry_run else answers, stdin_note="<answers file, not shown>",
            what="copy the answers file")
    bundle = None if r.dry_run else _bundle_text(source, args.ref)
    r.guest(user, ["bash", "-c", 'umask 077 && base64 -d > "$HOME/heading-os.bundle" '
                                 '&& chmod 600 "$HOME/heading-os.bundle"'],
            data=bundle, stdin_note=f"<bundle of {args.ref}, built only on a real run>",
            timeout=600, what="copy the engine bundle")
    script = ('rm -rf "$HOME/heading-src" '
              f'&& git clone -q --branch {ref} "$HOME/heading-os.bundle" "$HOME/heading-src" '
              f'&& bash "$HOME/heading-src/install.sh" --from "$HOME/heading-os.bundle" --ref {ref} '
              '--answers "$HOME/heading-answers.json" '
              f'--consent {shlex.quote(args.consent)}')  # HOS-340: a run from an answers file never asks
    run = r.guest(user, ["bash", "-c", script], check=False, timeout=None, what="install.sh")
    lines = ((run.stdout or "") + (run.stderr or "")).splitlines()
    for line in lines:
        print(line)
    status = next((line for line in reversed(lines) if line.startswith(STATUS_PREFIX)), None)
    state = None
    if status:
        print(status)
        try:
            state = json.loads(status[len(STATUS_PREFIX):]).get("state")
        except (ValueError, AttributeError) as exc:
            print(f"{RED}the HEADING-INSTALL line is not JSON ({exc}){RESET}", file=sys.stderr)
    # A login shell, as a new terminal runs it: `bash -c` reads no profile, so
    # its PATH lacks ~/.local/bin and lific, herdr, hindsight and claude read as
    # missing (HOS-312 rehearsal 2, 2026-11-03). The exit code is doctor's.
    doctor = r.guest(user, ["bash", "-lc", '"$HOME/HeadingOS/engine/.venv/bin/python" '
                                           '"$HOME/HeadingOS/engine/scripts/doctor.py"'],
                     check=False, timeout=600, what="doctor")
    if r.dry_run:
        return 0
    print(next((line for line in reversed((doctor.stdout or "").splitlines()) if line.strip()),
               "doctor printed nothing"))
    failed = []
    if state != "done":
        failed.append(f"the installer ended {state or 'with no HEADING-INSTALL line'}")
    if doctor.returncode != 0:
        failed.append(f"doctor exited {doctor.returncode}")
    if failed:
        print(f"{RED}install: {'; '.join(failed)}{RESET}", file=sys.stderr)
        return 1
    _ok("install: done, and doctor is green")
    return 0


def main(argv=None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dry-run", action="store_true",
                        help="print every command instead of running it")
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("reset", parents=[common], help="roll back to the clean snapshot and start")
    s.add_argument("--snapshot", help="snapshot to roll back to (default: the configured one)")
    sub.add_parser("status", parents=[common], help="VM state, snapshots, guest services")
    s = sub.add_parser("ssh", parents=[common], help="SSH to the guest through the host")
    s.add_argument("--user", help="guest user (default: the configured one)")
    s.add_argument("command", nargs=argparse.REMAINDER, help="-- command words")
    s = sub.add_parser("rebuild", parents=[common], help="reinstall the guest from nothing")
    s.add_argument("--storage-override", action="store_true",
                   help="create the VM on the configured disk_storage even when another "
                        "storage has more space")
    s.add_argument("--yes", action="store_true",
                   help="confirm: destroys the VM's current disk and replaces the snapshot")
    s = sub.add_parser("install", parents=[common], help="install HEADING OS on the guest, unattended")
    s.add_argument("--source", required=True, help="a local git repository holding an Official engine tree")
    s.add_argument("--ref", required=True, help="the branch or tag in it to install")
    s.add_argument("--answers", required=True, help="the intake answers, a JSON file of mode 600")
    s.add_argument("--consent", default="all",
                   help="passed to install.sh: all (the default), no, or a comma list of system, home, "
                        "downloads, services")
    s.add_argument("--yes", action="store_true", help="confirm: installs on the guest")
    args = p.parse_args(argv)

    cfg = load_config()
    runner = Runner(cfg, args.dry_run)
    handler = {"reset": cmd_reset, "status": cmd_status, "ssh": cmd_ssh,
               "rebuild": cmd_rebuild, "install": cmd_install}[args.cmd]
    try:
        return handler(cfg, runner, args)
    except (TestVmError, ValueError) as exc:
        print(f"{RED}{exc}{RESET}", file=sys.stderr)
        return 1
    except subprocess.TimeoutExpired as exc:
        print(f"{RED}timed out after {exc.timeout} s: {exc.cmd[0]} ...{RESET}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
