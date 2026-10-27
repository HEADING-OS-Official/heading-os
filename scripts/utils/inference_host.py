"""Resolve which inference host to talk to, with a fallback to the local one.

Why this exists. On a WSL2 workspace the fast server may live on the Windows
side (its GPU is reachable there, not inside WSL). Reaching it means addressing
the host across the NAT boundary, and that gateway address is NOT stable: WSL
picks a new one on restart. A gateway hardcoded in config therefore works until
the first reboot and then silently breaks every background timer that uses it.

So callers name an INTENT, not an address:

    resolve_inference_host("auto:11436")   # -> "http://<wsl-gateway>:11436"
    resolve_inference_host()               # -> "http://localhost:11434"

`auto:<port>` resolves the current default gateway at call time and probes it.
The probe is the point: a resolver that returns an address without checking it
would just move the silent breakage one layer down.

WHERE the preference comes from is `config/inference-hosts.yaml`, gitignored and
machine-local, with one entry for `embed` and one for `generate`. It is a
separate file from the tracked configs because an address is a fact about one
computer: `auto:11434` names whatever answers at THIS machine's default gateway,
and a pin written into a tracked file refuses on every clone that is not this
laptop. No file at all is a valid setup and means the local daemon.

Two resolvers, because two callers want opposite answers when nothing responds:

- `resolve_pinned_host` raises `InferenceHostUnavailable`. It is what both PINNED
  jobs use: EMBEDDING, and GENERATION through `generation_host`. The operator
  pinned a machine on 2026-08-23 and a quiet substitution is the failure, not
  the mitigation. Both take a list as readily as a string, so one pin can name
  several ports on the same machine.
- `resolve_inference_host` degrades to the local daemon. It survives for the
  unpinned embed callers that ask for a host by argument or environment
  variable (`scripts/utils/embeddings.py`, `scripts/memory-index.py`). No
  caller of it generates anything.

Sovereignty note: both candidates are on this machine. `auto` crosses the WSL
NAT boundary to the Windows side of the SAME laptop; it never reaches a network
host, and this module has no way to express one - only a gateway or a literal
the operator wrote themselves.

"ollama" here names a DIALECT as much as a program. Lemonade Server answers
`/api/embed`, `/api/tags` and `/api/version` in ollama's own wire format, byte
for byte, and differs in exactly two things: the address, and a required
`Authorization: Bearer` header (measured in HELM 2026-09-23). So the address
resolvers below are unchanged and the header is threaded through as an argument.
Which program answers is DECLARED, in `config/inference-hosts.yaml` or in
`config/memory-index.yaml`, and is never inferred from a port number: a guess
keyed on the address is wrong the first time the address moves.

The one place the two DO diverge is a completion, which Lemonade serves on the
OpenAI-compatible surface under its own `/api/v1` prefix rather than on ollama's
`/api/generate`. Nothing in this module builds a completion, so nothing here
changes; what this module owns is the name of the program, read by whichever
caller has to choose a dialect (`generation_provider` below, and
`scripts/utils/embeddings.index_embed_provider` for the embed side).
"""

from __future__ import annotations

import json
import os
import socket
import struct
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import urlsplit

LOCAL_HOST = "http://localhost:11434"
PROC_ROUTE = "/proc/net/route"

# Where THIS machine says its inference host lives. Gitignored, with a tracked
# `config/inference-hosts.example.yaml` beside it that documents the shape and
# switches nothing on.
#
# It is a separate file because the pin is a fact about one laptop, not about
# the repository. It lived in `config/memory-index.yaml` for one day and that
# was a day too long: that file is tracked, `auto:11434` means "whatever answers
# at this clone's default gateway", and since a pin refuses instead of degrading,
# the shipped default made `memory-index build` fail on any clone that was not
# this WSL2 laptop -- while its own local ollama sat there working.
MACHINE_HOSTS_FILE = "config/inference-hosts.yaml"

# What this file was called until 2026-09-24. Held here so `_machine_payload`
# can SAY that an unrenamed pin is being ignored, and for nothing else: it is
# never opened, never parsed, and never falls back to. See
# `_report_legacy_machine_file` for why that direction was chosen.
LEGACY_MACHINE_HOSTS_FILE = "config/ollama-hosts.yaml"
_LEGACY_REPORTED = False

# The two one-off overrides, and what each was called until 2026-09-24. The old
# pair named one job and not the other, so a reader met `HEADING_OS_OLLAMA_HOST`
# beside `HEADING_OS_OLLAMA_EMBED_HOST` and had to open the code to learn which
# job the bare one moved. The new pair spells both roles, the same two words the
# machine file spells them with: `generate:` and `embed:`.
GENERATE_HOST_ENV = "HEADING_OS_INFERENCE_GENERATE_HOST"
EMBED_HOST_ENV = "HEADING_OS_INFERENCE_EMBED_HOST"
RENAMED_HOST_ENV = {
    GENERATE_HOST_ENV: "HEADING_OS_OLLAMA_HOST",
    EMBED_HOST_ENV: "HEADING_OS_OLLAMA_EMBED_HOST",
}

# One entry per job, because each job pins its own host: embedding through
# `resolve_pinned_host`, generation through `generation_host`, and both refuse
# rather than degrade when the pinned host is down.
MACHINE_HOST_ROLES = ("embed", "generate")

# Scalar keys the same file may carry, for facts about the machine that are not
# addresses. `<job>_provider` names the PROGRAM answering at that job's address
# ("ollama", "lemonade"); `<job>_api_key_env` names the environment variable
# holding that program's bearer token, and never the token. `generate_model`
# names the model the generation job asks for. All are optional and all default
# to today's behaviour when absent: ollama, unauthenticated, `gemma3:4b`.
#
# The generation three arrived 2026-09-23, when the operator asked for the
# NPU-backed model. They sit beside the addresses rather than in a tracked file
# for the same reason the addresses do: a serving program, a credential and a
# model that only exists on a machine with an NPU are facts about ONE computer.
MACHINE_SETTING_KEYS = (
    "embed_provider", "embed_api_key_env",
    "generate_provider", "generate_api_key_env", "generate_model",
)

# The two serving programs this workspace has measured. A completion is the one
# request whose SHAPE differs between them, so the name is what a caller reads
# before it builds one; see the module docstring.
PROVIDER_OLLAMA = "ollama"
PROVIDER_LEMONADE = "lemonade"

# What `generate_model` means when nothing declares one. This is the literal
# `scripts/chronicle.py` carried as `MODEL` until 2026-09-23, kept as the default
# so a clone that declares nothing summarizes exactly as it did before.
GENERATION_MODEL_DEFAULT = "gemma3:4b"

# Kept short on purpose: this runs before real work, in daemons that must not
# hang when the Windows side is simply not running.
DEFAULT_PROBE_TIMEOUT = 2.0


class InferenceHostUnavailable(RuntimeError):
    """No pinned inference host answered, and degrading was not permitted."""


def read_default_gateway(route_path: str = PROC_ROUTE) -> str | None:
    """Return the IPv4 default gateway, or None when it cannot be determined.

    Parses /proc/net/route rather than shelling out to `ip route`, so there is
    no subprocess and nothing to quote. The Gateway column is a little-endian
    hex word; the default route is the row whose Destination is all zeroes.
    """
    try:
        with open(route_path, encoding="utf-8") as fh:
            rows = fh.read().splitlines()
    except OSError:
        return None

    for line in rows[1:]:                      # first line is the header
        fields = line.split()
        if len(fields) < 3:
            continue
        destination, gateway = fields[1], fields[2]
        if destination != "00000000":
            continue
        try:
            packed = struct.pack("<L", int(gateway, 16))
        except (ValueError, struct.error):
            continue
        return socket.inet_ntoa(packed)
    return None


def is_http_url(value: str) -> bool:
    """True for an http/https URL with a host part.

    The candidate reaches here from an environment variable or a config file,
    and `urlopen` honours whatever scheme it is handed - `file:///etc/passwd`
    would be opened and read. Only http(s) can ever be an inference endpoint, so
    everything else is rejected before it reaches the opener.
    """
    parts = urlsplit(value)
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def probe(host: str, timeout: float = DEFAULT_PROBE_TIMEOUT, *, headers=None) -> bool:
    """True when a server answers /api/version at `host`.

    `headers` is optional and is supplied by whichever caller's host is gated by
    a key. It exists because a server can speak ollama's dialect behind an API
    key -- Lemonade Server answers `/api/version` with `{"version": ...}` and
    answers 401 to the same request unauthenticated (measured in HELM
    2026-09-23). Without the header this function reads a healthy server as
    dead, `resolve_pinned_host` raises `InferenceHostUnavailable` before the first
    real call, and the job goes down at the moment the address changes.

    It is deliberately NOT resolved inside this function from configuration.
    Two jobs resolve their hosts through here, EMBEDDING and GENERATION, each
    with its own declared provider and its own key variable; a header this
    function fetched for itself would have to pick one of them, and a key on a
    wire that does not need one is a leak with no upside.
    """
    if not is_http_url(host):
        return False
    url = f"{host.rstrip('/')}/api/version"
    try:
        # Scheme is checked immediately above, so the opener only ever sees
        # http/https here.
        request = urllib.request.Request(url, headers=dict(headers or {}))  # noqa: S310
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            payload = json.loads(response.read().decode())
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return False
    # `isinstance`, not `in`. A 200 carrying a JSON SCALAR - `null`, a number, a
    # bool - parses fine and then `"version" in payload` raises TypeError, which
    # the clause above does not catch. The caller got a bare TypeError out of a
    # function whose entire contract is to answer True or False, so
    # `resolve_inference_host` could not fall back to the local daemon and
    # `resolve_pinned_host` could not raise `InferenceHostUnavailable` naming what
    # it tried. Reproduced 2026-08-25 against a loopback server returning
    # `null`. `auto:<port>` invites exactly this: the WSL gateway address with
    # some other service on that port.
    return isinstance(payload, dict) and "version" in payload


@dataclass
class ProbeTarget:
    """One address together with the credential its job has to send.

    The shared root of a defect that was five copies of itself. `probe` has
    accepted `headers=` since 2026-09-23, and MEASURED 2026-09-24 exactly two of
    its seven callers passed one. The other five probed a key-gated Lemonade
    unauthenticated, got 401, and reported a healthy server as dead -- because
    `HTTPError` subclasses `URLError`, and every one of those handlers reads a
    `URLError` as "nothing answered".

    Threading `headers=` into those five would have repaired those five. This
    repairs the SHAPE: a resolver hands back an address that is ALREADY ARMED,
    so the sixth caller written next month cannot hold an address without its
    credential. It is also the only shape that is correct here, because a single
    `headers` argument threaded through a list of addresses would be wrong on
    its face: this workspace runs two jobs, EMBEDDING and GENERATION, each
    declaring its own key variable, and a monitor watching both is handed a list
    drawn from both.

    Note the asymmetry that makes the fix worth placing at the root. A probe
    that omits the credential fails CLOSED in appearance, reporting "down" for
    something healthy, which reads as conservative and is the more expensive
    direction: it makes a healing action fire against a server that needs no
    healing.

    `auth_error` says why `headers` is empty when a key variable WAS declared
    and nothing has set it. That is a different failure from an unreachable host
    and needs a different repair, so it gets its own sentence rather than being
    folded into "down" -- the same reasoning `classify_ollama_accel` gives for
    `pin_unresolvable`. It carries the VARIABLE NAME and never the key, which is
    all `scripts/utils/api.load_api_key` puts in its own message.
    """

    url: str
    # `repr=False` is not cosmetic. `headers` holds `Authorization: Bearer
    # <key>`, and a dataclass repr is what a debug print, a traceback frame, a
    # `--json` dump built from `str()`, and an exception message all reach for.
    # MEASURED 2026-09-24 while this class was being written: printing a list of
    # targets put the live key on the terminal. The one field that must never be
    # displayed is the one every default display would have shown.
    headers: dict = field(default_factory=dict, repr=False)
    auth_error: str = ""

    def reachable(self, timeout: float = DEFAULT_PROBE_TIMEOUT) -> bool:
        """`probe` against this address, carrying this address's credential.

        Named `reachable` rather than `probe` so no reader has to work out
        whether the module-level function is being shadowed inside the class.
        """
        return probe(self.url, timeout, headers=self.headers)


def auto_pin_port(value: str) -> str | None:
    """The port an `auto` gateway pin names, or None when it names none.

    None covers three cases, and the caller treats them alike because all three
    mean "this string does not name the gateway": the value is not an `auto`
    pin at all, its port is not a number, or its number is not a port.

    The membership test used to be `value.startswith("auto")`, which is a prefix
    where the documented forms are exactly `auto` and `auto:<port>`. MEASURED
    2026-08-29 on this laptop: `candidate_url("autobahn")` returned
    `http://<wsl-gateway>:11434` and `candidate_url("automotive:9000")` returned
    `http://<wsl-gateway>:9000`. A misspelt pin did not fail, it silently
    repointed at the gateway, and a probe against whatever answers there decides
    where embedding runs. The port was unvalidated too, so `auto:banana` built
    `http://<wsl-gateway>:banana` and `resolve_inference_host` then reported that
    malformed pin as "unreachable" -- a wrong address, not a wrong string.
    """
    if value != "auto" and not value.startswith("auto:"):
        return None
    _, _, port = value.partition(":")
    port = port.strip() or "11434"
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        return None
    return port


def candidate_url(preferred: str | None) -> str | None:
    """Turn a preference into the address it names, WITHOUT probing it.

    Returns None when there is no usable preference: an empty value, an `auto`
    whose gateway cannot be read, or anything that is not an http(s) URL.

    Split out of `resolve_inference_host` so a caller can tell "no accelerated host
    is configured" from "one is configured and is down". The resolver folds both
    into the same local fallback, which is right for the callers that only want
    a working endpoint and useless for a monitor - that fold is exactly how a
    16-hour outage of the Windows-side daemon went unreported on 2026-08-21.
    """
    wanted = (preferred or "").strip()
    if not wanted:
        return None

    port = auto_pin_port(wanted)
    if port is not None:
        gateway = read_default_gateway()
        if gateway is None:
            return None
        return f"http://{gateway}:{port}"

    candidate = wanted.rstrip("/")
    return candidate if is_http_url(candidate) else None


def host_candidates(preferred) -> list[str]:
    """Every address a preference names, in order, WITHOUT probing any of them.

    `preferred` is a string or a list of strings. A list exists because one
    machine can serve on more than one port: the Windows Ollama desktop app
    binds its default 11434 unless it was launched with an explicit
    `OLLAMA_HOST`, and on 2026-08-23 an auto-update restart did exactly that
    while the pin still said 11436. Naming both ports makes that whole class of
    failure a non-event, and neither of them is the local daemon, so the pin
    still means one machine.

    Entries that cannot become an http(s) URL are dropped rather than raising:
    one typo must not disable the entries beside it. Duplicates collapse, so
    `["auto:11434", "http://<gateway>:11434"]` probes once.
    """
    values = preferred if isinstance(preferred, (list, tuple)) else [preferred]
    out: list[str] = []
    for value in values:
        url = candidate_url(value) if isinstance(value, str) else None
        if url and url not in out:
            out.append(url)
    return out


def main_worktree_root(root):
    # No return annotation: this module imports `Path` inside each function
    # rather than at module scope, and naming it here is an undefined name
    # (ruff F821) even inside a string annotation.
    """The main checkout behind a LINKED WORKTREE at `root`, else None.

    A linked worktree's `.git` is a FILE reading `gitdir: <common>/worktrees/<n>`,
    where `<common>` is the main checkout's `.git` directory. A normal clone's
    `.git` is a directory, and this returns None for it, so nothing about the
    ordinary case changes.

    Read from the checkout's own `.git`, never from a constant, an environment
    variable, or `git rev-parse`: `CLAUDE.md` requires anything resolving a tree
    to derive it from the current checkout, and a subprocess here would run on
    every embed call.
    """
    from pathlib import Path

    root = Path(root)
    dotgit = root / ".git"
    try:
        if not dotgit.is_file():          # a directory, or absent: ordinary clone
            return None
        text = dotgit.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    prefix = "gitdir:"
    line = next((ln.strip() for ln in text.splitlines()
                 if ln.strip().startswith(prefix)), "")
    if not line:
        return None

    gitdir = Path(line[len(prefix):].strip())
    if not gitdir.is_absolute():
        gitdir = (root / gitdir).resolve()

    # `<common>/worktrees/<name>` -> `<common>` -> the checkout holding it.
    if gitdir.parent.name != "worktrees":
        return None
    common = gitdir.parent.parent
    if common.name != ".git":
        return None
    main = common.parent
    return main if main.is_dir() and main != root else None


def _report_legacy_machine_file(root) -> None:
    """Say so, once per process, when the pin exists only under its OLD name.

    **This is not a fallback.** It reads nothing and changes nothing: the pin
    stays unresolved and the caller keeps the documented "this machine pins
    nothing" default. What it removes is the SILENCE, and that is the whole of
    its job.

    Why it is here at all. The files were renamed on 2026-09-24, when ollama
    stopped being installed on the operator's machine and both jobs moved to a
    server that only speaks its dialect. `MACHINE_HOSTS_FILE` is gitignored and
    machine-local, so nothing in a merge can rename it; a person does, by hand,
    at some moment either side of the merge. Between those two moments the
    pin is invisible to this module, every job resolves to the local daemon, and
    on a machine where no local daemon is installed the failure surfaces two
    layers down as a connection error against an address nobody configured.

    Why not simply read the old file. Because a silent fallback hides that the
    rename did not finish, and then nothing ever makes it finish: the old name
    survives for as long as it keeps working, which is forever. A line naming
    the exact `mv` is louder, costs one stderr write, and clears itself the
    moment somebody runs it.

    An ordinary clone that never had either file hears nothing, which is the
    documented default and the state of every fresh checkout and of CI.
    """
    global _LEGACY_REPORTED
    if _LEGACY_REPORTED:
        return

    from pathlib import Path

    roots = [Path(root)]
    main = main_worktree_root(root)
    if main is not None:
        roots.append(main)

    for candidate in roots:
        legacy = candidate / LEGACY_MACHINE_HOSTS_FILE
        if legacy.exists():
            _LEGACY_REPORTED = True
            sys.stderr.write(
                f"inference: {legacy} is the PREVIOUS name of this pin and is "
                f"not read. Nothing is pinned until it is renamed:\n"
                f"  mv {legacy} {candidate / MACHINE_HOSTS_FILE}\n"
            )
            return


def env_override(name: str) -> str:
    """The one-off override under `name`, refusing if its OLD name is set.

    Every environment read of an inference address goes through here, so the
    refusal below cannot be forgotten by a call site added later.

    Returns:
        The stripped value, or "" when nothing is set. "" is the normal state
        and the documented default: the machine file answers instead.

    Raises:
        InferenceHostUnavailable: the PRE-RENAME name is set. Nothing reads it,
            and it is not read here either.

    Why this refuses where `_report_legacy_machine_file` only reports, given
    that both guard the same rename. The two failures are not the same shape. A
    machine file under its old name leaves the pin UNRESOLVED, so the job either
    refuses a moment later naming the address or falls to the documented local
    default; the damage is visible. An override under its old name leaves the
    job RESOLVING NORMALLY, against the host the operator did not choose, and
    succeeding. Success is the worse outcome, because nothing downstream has a
    reason to complain.

    And a quiet fallback is not available here even in principle. An unset
    override is a supported, ordinary state, so "the old name was silently
    ignored" and "nobody set anything" produce byte-identical behaviour. The
    only way to tell a reader which one happened is to stop.
    """
    legacy = RENAMED_HOST_ENV.get(name)
    if legacy and os.environ.get(legacy, "").strip():
        raise InferenceHostUnavailable(
            f"{legacy} is set and nothing has read it since 2026-09-24. "
            f"It was renamed to {name}; rename it where you set it. "
            f"Its value is NOT read as a fallback, because an override that is "
            f"quietly ignored cannot be told apart from one nobody set."
        )
    return os.environ.get(name, "").strip()


def _machine_payload(root=None) -> dict:
    """`config/inference-hosts.yaml` as a mapping. {} when it says nothing readable.

    Split out of `machine_hosts` on 2026-09-23, when a SECOND kind of
    machine-local fact arrived beside the two address lists: which PROGRAM
    serves the embed address, and which environment variable holds its key. One
    reader, so the linked-worktree fallback below and the report-then-ignore
    behaviour on a bad file cannot come to exist in two versions that disagree.

    One exception to "an absent file means this machine pins nothing", and it is
    not a widening of that default: when `root` is a LINKED WORKTREE and holds
    no file of its own, the main checkout's file is read instead. A worktree is
    the same machine, so "absent here" means "not visible from here".
    """
    from pathlib import Path

    import yaml

    from scripts.utils import yamlio

    if root is None:
        from scripts.utils.workspace import get_workspace_root

        root = get_workspace_root()

    path = Path(root) / MACHINE_HOSTS_FILE
    if not path.exists():
        # A linked worktree is the SAME machine as the checkout it was cut from,
        # and this file is a fact about the machine. The pin is gitignored for a
        # good reason (every address in it names one laptop), but that makes it
        # absent in every worktree, where all engine work happens.
        #
        # MEASURED 2026-09-06 in `yard-memory-recalibration`, before this branch
        # existed: `memory-index.py query` returned `{"hits": [], "gap": true,
        # "embed_unavailable": {... cannot reach embedder at
        # http://localhost:11434 ...}}`, because an absent file resolved to the
        # unprobed local default and the local daemon was removed 2026-08-23.
        # Memory search was dead in every yard and said nothing about it.
        #
        # Absence in an ordinary clone still means "this machine pins nothing",
        # which is the documented, tested default: `main_worktree_root` returns
        # None there and this branch changes nothing.
        main = main_worktree_root(root)
        if main is not None:
            path = main / MACHINE_HOSTS_FILE
    if not path.exists():
        _report_legacy_machine_file(root)
    try:
        with open(path, encoding="utf-8") as fh:
            payload = yamlio.safe_load(fh) or {}
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        # Reported, never swallowed: a typo here silently unpins the machine,
        # and a silent unpin is the whole failure this arrangement exists to
        # prevent.
        #
        # `UnicodeDecodeError` is a `ValueError` and a SIBLING of `YAMLError`,
        # not a subclass of either clause above it, and the decode happens
        # inside the READ that `yamlio.safe_load` performs, before any parse.
        # MEASURED 2026-09-01 with `embed: [\xff'auto:11434']` in the machine
        # file: `machine_hosts("embed")` RAISED instead of reporting and
        # returning [], so one bad byte in a gitignored, hand-edited,
        # machine-local file took down every caller (`generation_host` for the
        # 03:00 chronicle build, `index_embed_target` for `memory-index build`
        # rather than falling back to no pin. The docstring above promises
        # the reported-then-ignored behaviour; this is the class that escaped it.
        sys.stderr.write(f"inference: cannot read {MACHINE_HOSTS_FILE}: {exc}\n")
        return {}

    if not isinstance(payload, dict):
        sys.stderr.write(f"inference: {MACHINE_HOSTS_FILE} is not a mapping, ignoring it\n")
        return {}

    return payload


def machine_hosts(role: str, *, root=None) -> list[str]:
    """This machine's preference for `role`, as raw entries. [] when unset.

    Reads `config/inference-hosts.yaml` (see `MACHINE_HOSTS_FILE`). An absent file
    means no accelerator here, which is the right default for a fresh clone, for
    CI, and for this laptop before its Windows side is running. Returns the
    entries UNRESOLVED so the caller picks its own resolver - refusing for
    embedding, degrading for generation.

    Args:
        role: one of `MACHINE_HOST_ROLES`.
        root: workspace root; resolved from the workspace when omitted.

    Raises:
        ValueError: unknown role. A typo must not read as "nothing configured".
    """
    if role not in MACHINE_HOST_ROLES:
        raise ValueError(
            f"unknown inference host role {role!r}; expected one of {MACHINE_HOST_ROLES}"
        )

    value = _machine_payload(root).get(role)
    if value is None:
        return []
    values = value if isinstance(value, (list, tuple)) else [value]
    # Non-strings are dropped rather than raising, for the same reason
    # `host_candidates` drops a bad URL: one bad row must not disable the good
    # ones beside it.
    return [v.strip() for v in values if isinstance(v, str) and v.strip()]


def machine_setting(key: str, *, root=None) -> str:
    """One scalar machine-local fact from `config/inference-hosts.yaml`. "" when unset.

    The same file as `machine_hosts` and for the same reason: an address, the
    program answering at it, and the name of the variable holding that
    program's key are all facts about ONE computer. `config/memory-index.yaml`
    is tracked and ships to every clone, so a value written there is a claim
    about every clone -- which is exactly how a pin in that file broke
    `memory-index build` everywhere that was not this laptop for a day in
    August. The tracked file still WINS when it is filled in; this is where the
    answer belongs when it is not.

    Raises:
        ValueError: unknown key, for the reason `machine_hosts` gives.
    """
    if key not in MACHINE_SETTING_KEYS:
        raise ValueError(
            f"unknown inference host setting {key!r}; expected one of {MACHINE_SETTING_KEYS}"
        )
    value = _machine_payload(root).get(key)
    return value.strip() if isinstance(value, str) else ""


def generation_provider(*, root=None) -> str:
    """Which PROGRAM serves the generation host: "ollama" (default) or "lemonade".

    DECLARED, never inferred from the address, for the reason
    `scripts/utils/embeddings.index_embed_provider` gives about the embed side:
    a resolver that concluded "13305 means Lemonade" would be guessing, and
    would be wrong the first time the port moved -- which is the whole failure
    the `auto:` pin exists to end for addresses.

    What it decides is the DIALECT of a completion, which is the one request the
    two programs do not share. Everything else in this module is dialect-blind,
    because Lemonade answers `/api/version`, `/api/embed` and `/api/tags` in
    ollama's own wire format (measured in HELM 2026-09-23).

    There is no tracked file that can override it, unlike the embed side's
    `config/memory-index.yaml`. That file describes the memory INDEX and a
    summarizer model is no part of one, so a value written there would be a
    claim about every clone in a file about something else. The asymmetry is
    deliberate and is the reason this reads one source where
    `index_embed_provider` reads two.
    """
    return machine_setting("generate_provider", root=root) or PROVIDER_OLLAMA


def generation_api_key_env(*, root=None) -> str:
    """The NAME of the variable holding the generation host's key. "" when none.

    The name, never the value, exactly as on the embed side: the value is a
    secret read by `scripts/utils/api.load_api_key` from the environment or a
    gitignored `.env`, and nothing in this module returns, prints or logs it.

    Empty means the host authenticates nothing, which is ollama, and which is
    what every clone that leaves this alone keeps.
    """
    return machine_setting("generate_api_key_env", root=root)


def generation_model(*, root=None) -> str:
    """The model the local summarizer asks for. `GENERATION_MODEL_DEFAULT` when unset.

    A second hard-coded constant would be the first one wearing the other hat,
    so this is read from configuration and moving between models is an edit to
    `config/inference-hosts.yaml` rather than to code.
    """
    return machine_setting("generate_model", root=root) or GENERATION_MODEL_DEFAULT


def generation_auth_headers(*, root=None) -> dict:
    """Headers every request to the generation host carries. {} when it needs none.

    Declaring `generate_api_key_env` and then leaving that variable unset is a
    HARD failure, not a silent unauthenticated request: `load_api_key` raises,
    naming the variable and nothing else. Degrading to no header produces a 401
    that `probe` reads as "nothing answered", which sends the reader to restart
    a server that is already running.

    Same shape and same reasoning as `scripts/utils/embeddings.embed_auth_headers`.
    They are two functions rather than one because they read two different keys
    for two different hosts, and a single parameterised reader would still need
    the caller to name which job it is.
    """
    env_name = generation_api_key_env(root=root)
    if not env_name:
        return {}

    from scripts.utils.api import load_api_key

    return {"Authorization": f"Bearer {load_api_key(env_name)}"}


def unpinned_local_host(
    role: str,
    *,
    headers=None,
    probe_timeout: float = DEFAULT_PROBE_TIMEOUT,
) -> str:
    """`LOCAL_HOST`, but only once something has answered there.

    The unpinned default exists for a plain clone that runs one ollama and
    should need no setup, and that case is preserved exactly: if a daemon
    answers, this returns the same address it always did.

    What it stops is the other case. Both resolvers used to return `LOCAL_HOST`
    UNPROBED whenever nothing was pinned, which was safe only while an ollama
    was installed. Operator directive 2026-09-24, after a drain pass was sent to
    port 11434 on a machine where ollama had been removed that day: inference on
    this machine goes to Lemonade and nowhere else. An address handed back
    without a probe is not a default, it is a guess, and this one had become a
    guess about a program that is gone.

    The cost of the old shape was paid downstream and late. `index_embed_target`
    returned `http://localhost:11434` three calls out of three, every request to
    it was refused, and the failure surfaced about thirty seconds later inside
    the graph-memory pilot of the day (removed 2026-10-04) as `Embedding
    connection test timed out after 30s` -- a message that
    names neither the address nor the missing pin, and that reads as a broken
    embedder rather than as absent configuration. MEASURED 2026-09-24.

    So the refusal names all three things a reader needs: the role, the address
    nobody answered at, and the file that fixes it.

    Raises:
        InferenceHostUnavailable: nothing is pinned for `role` and nothing
            answers at `LOCAL_HOST`.
    """
    if probe(LOCAL_HOST, probe_timeout, headers=headers):
        return LOCAL_HOST
    raise InferenceHostUnavailable(
        f"no host is pinned for {role!r} and nothing answers at {LOCAL_HOST}. "
        f"Pin it in config/inference-hosts.yaml, e.g. "
        f'{role}:\n  - "auto:13305"\n{role}_provider: "lemonade"'
    )


def generation_host(
    *,
    root=None,
    probe_timeout: float = DEFAULT_PROBE_TIMEOUT,
) -> str:
    """Where local GENERATION runs. A PIN: it refuses, never degrades.

    Precedence: `HEADING_OS_INFERENCE_GENERATE_HOST` (a one-off override)
    beats `generate:` in the machine file. Nothing set anywhere returns the
    local daemon once a probe of it answers (`unpinned_local_host`), so a plain
    clone with one local daemon works with no setup.

    Raises:
        InferenceHostUnavailable: a host is pinned and nothing it names answers.

    This refused to be a pin for about an hour on 2026-08-23, on the argument
    that a summary from the CPU copy of the same model is the same summary and
    the nightly run must produce a record. The operator closed the argument by
    removing its premise: there is no second copy. The ollama inside WSL is gone,
    every model this workspace uses lives on the Windows side, and a "fallback to
    the local daemon" now falls back to nothing at all. A refusal that names the
    dead host beats a connection error from a daemon that is not installed.

    The cost is stated rather than hidden: on a night the Windows side is asleep,
    the 03:00 chronicle build fails loudly and writes no record for that day.

    **The probe carries the same credential the generation call will**, since
    2026-09-23. Without it a key-gated host answers 401 to `/api/version`,
    `probe` reads that as dead, and this function raises before the first
    completion is ever attempted -- the whole chronicle down, produced by the
    health check rather than by the model. The embed side reached the identical
    wall and solved it the identical way in
    `scripts/utils/embeddings.index_embed_target`; this is that repair placed at
    the resolver both of THIS function's callers already share, so
    `scripts/chronicle.py` and `scripts/census-submodel-bench.py` get it without
    either of them learning what a credential is. A host that declares no key
    variable resolves `{}` here and is probed exactly as it was before.

    Raises:
        ValueError: `generate_api_key_env` names a variable nothing has set.
            From `load_api_key`, which names the variable and never its value.

    CALL THIS LAZILY, never at module scope: it probes.
    """
    override = env_override(GENERATE_HOST_ENV)
    preference = override or machine_hosts("generate", root=root)
    headers = generation_auth_headers(root=root)
    if not preference:
        # Probed since 2026-09-24 rather than returned on faith; see
        # `unpinned_local_host`, and the paragraph above about there being no
        # second copy of anything on this machine any more.
        return unpinned_local_host(
            "generate", headers=headers, probe_timeout=probe_timeout
        )
    return resolve_pinned_host(
        preference,
        probe_timeout=probe_timeout,
        headers=headers,
    )


def resolve_pinned_host(
    preferred,
    *,
    probe_timeout: float = DEFAULT_PROBE_TIMEOUT,
    headers=None,
) -> str:
    """The first pinned host that answers. Never degrades to the local daemon.

    Args:
        headers: passed through to `probe`. Supplied by whichever caller's host
            is key-gated -- `scripts/utils/embeddings.index_embed_target` for
            embedding, `generation_host` for generation -- and empty for a host
            that authenticates nothing (see `probe`).

    Raises:
        InferenceHostUnavailable: nothing the preference names is reachable. The
            message lists every address tried, because "the embedder is down" is
            unactionable and "10.0.0.1:11434 and :11436 did not answer" is not.

    Both PINNED jobs come through here: embedding, and generation via
    `generation_host`. `resolve_inference_host` below is the degrading resolver the
    unpinned embed callers use.
    """
    wanted = preferred.strip() if isinstance(preferred, str) else preferred
    if not wanted:
        # Nothing pinned. The local daemon is the default, and probing it here
        # would only turn "no accelerator configured" into a hard failure on
        # every machine that never had one - a public clone, or CI.
        return LOCAL_HOST

    candidates = host_candidates(wanted)
    if not candidates:
        raise InferenceHostUnavailable(
            f"no usable inference host in the pin {preferred!r} "
            f"(an `auto:` pin needs a readable default gateway)"
        )
    if all(c == LOCAL_HOST for c in candidates):
        # Naming the local daemon is not a pin, it is the default written out.
        # Unprobed for the same reason as above; if it is down, the embed call
        # itself says so a moment later and says it better.
        return LOCAL_HOST

    for candidate in candidates:
        if probe(candidate, timeout=probe_timeout, headers=headers):
            return candidate
    raise InferenceHostUnavailable(
        "no pinned inference host answered: " + ", ".join(candidates)
    )


def resolve_inference_host(
    preferred: str | None = None,
    *,
    env_var: str = GENERATE_HOST_ENV,
    default: str = LOCAL_HOST,
    probe_timeout: float = DEFAULT_PROBE_TIMEOUT,
    verbose: bool = True,
    headers=None,
) -> str:
    """Pick an inference base URL: the wanted one if it answers, else `default`.

    Args:
        preferred: `auto:<port>` to resolve the current gateway, a literal base
            URL, or None to read `env_var` (and fall back to `default`).
        env_var: environment variable consulted when `preferred` is None.
        default: what to use when nothing else is reachable. Never probed - it
            is the fallback, and probing it would only add a delay to the path
            that is already the last resort.
        probe_timeout: seconds to wait for /api/version.
        verbose: write one line to stderr when a preference is dropped, so a
            silent downgrade cannot masquerade as a fast run.
        headers: passed through to `probe`, for the embed callers whose host
            requires an API key. See `probe` for why this is not read from
            configuration here.

    Returns:
        A base URL with no trailing slash.
    """
    wanted = preferred if preferred is not None else env_override(env_var)
    if isinstance(wanted, str):
        wanted = wanted.strip()
    if not wanted:
        return default.rstrip("/")

    candidates = host_candidates(wanted)
    if not candidates:
        # `auto_pin_port`, not `startswith("auto")`: only a pin that really
        # names the gateway can have failed for want of a gateway. `autobahn`
        # and `auto:banana` used to be blamed on an unreadable route table,
        # which sends the reader to /proc/net/route over a typo in the pin.
        reason = (
            "cannot read default gateway"
            if isinstance(wanted, str) and auto_pin_port(wanted) is not None
            else f"{wanted!r} is not an http(s) URL"
        )
        _warn(verbose, f"inference: {reason}, using {default}")
        return default.rstrip("/")

    for candidate in candidates:
        if probe(candidate, timeout=probe_timeout, headers=headers):
            return candidate

    _warn(
        verbose,
        f"inference: {', '.join(candidates)} unreachable, falling back to {default}",
    )
    return default.rstrip("/")


def _warn(verbose: bool, message: str) -> None:
    if verbose:
        sys.stderr.write(message + "\n")
