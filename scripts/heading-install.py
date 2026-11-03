#!/usr/bin/env python3
"""Install HEADING OS for an executive: one resumable, idempotent installer.

Leaves, under one visible folder (default ~/HeadingOS):
  <target>/engine   the engine, cloned directly at the latest release tag
  <target>/data     YOUR private data repository, local by default

Every step prints one plain line. A rerun skips what is already in place and
repairs what is broken: each step asks the machine whether its result is there,
and the state file (<target>/.heading-install/state.json) only records when a
step last finished. Steps are a registry (`STEPS`, filled by `@step`); later
slices add theirs (secrets, modules, services, doctor, launcher) by appending a
decorated function in the place marked below.

Standard library only, Python 3.11, so it runs before the engine and its
environment exist: `install.sh` gets git and uv, then runs this file through
`uv run --python 3.11 --no-project`.

Usage:
  python scripts/heading-install.py [--target DIR] [--mail CHOICE]
                                    [--github-backup yes|later] [--tz ZONE]
                                    [--proxy-signin now|later] [--answers PATH]
                                    [--repo-name NAME] [--ref TAG]
                                    [--from GIT_URL] [--dry-run]

  --target DIR     where engine/ and data/ go (default: ~/HeadingOS)
  --mail exchange|google|none
                   an intake answer: connect an Exchange mailbox and calendar,
                   Gmail and Google Calendar, or none (connect later)
  --github-backup yes|later
                   an intake answer: yes also keeps a private copy of the data
                   on your own GitHub account (an existing private repository
                   of that name is adopted, not a failure); later keeps the
                   data repository on this computer only. `--github` is a
                   hidden alias of `--github-backup yes`
  --tz ZONE        an intake answer: your time zone, an IANA name such as
                   Europe/Berlin
  --proxy-signin now|later
                   the Claude sign-in for the model proxy: now (the default) or later
  --answers PATH   read the intake answers from a JSON file (mode 600) and never stop to ask
  --repo-name NAME the GitHub repository name (default: heading-os-data)
  --ref TAG        install this tag or branch instead of the latest release
  --from GIT_URL   clone the engine from here (default: the official repository)
  --dry-run        say what each step would do; change nothing
  --google-client PATH
                   with --mail google: the client file downloaded from your own
                   Google sign-in app
  --google-audience production|internal
                   with --mail google: the publishing status you read on the
                   Google console's Audience page

The intake (HOS-312). Before the first step runs, dry run included, each of the
three intake answers comes from its flag, else from the engine's .env when a
previous run stored it there. The first answer still missing, in the order
mail, GitHub backup, time zone, ends the run before anything is installed. The
answers are stored in .env only, by the steps `answers`, `timezone` and
`mail-choice`; a flag given on a rerun replaces the stored value. The fourth
answer, the Claude sign-in for the model proxy, is never asked, and is `now`
unless a flag, the answers file or the engine .env says `later`.

Every run past argument parsing ends with one line on stdout:
  HEADING-INSTALL: {"state": ..., "say": ..., "next": ...}
  state need-answer (exit 3): `say` is the question, `options` the allowed
    answers; `next` is this command with the asked flag's value left as the
    bare token {answer}, never inside quotes. The caller replaces that one
    bare word (never a {answer} inside a quoted word)
    with the chosen value SHELL-QUOTED: the value wrapped in single quotes,
    each single quote inside it written as '"'"' (what Python's shlex.quote gives). The
    run then receives the value as exactly one argument, with no expansion of
    a space, $, a backtick or a quote. An option value "{free}" means: ask the
    executive and use their own words as the value, quoted the same way
  state waiting-for-person (exit 4): a window or a browser page is open for
    the person (a secret window, a sign-in). Claude relays `say`, waits for
    the person to say they are done, then runs `next`, this same command. For
    the Claude sign-in only, when the person says they would rather do it
    later, Claude may append ` --proxy-signin later` to `next`, the one change
    to a `next` it may make. No
    install report is written: a person being needed is not a failure. Steps
    raise it through scripts/utils/install_person.py (HOS-312)
  state failed (exit 1; exit 2 for a refused --target or a refused or
    incomplete --answers file, with no report): `say` is the reason,
    `next` reruns the same command; the details are in
    <target>/install-report.txt (mode 600, no secret and no other .env value)
  state done (exit 0): after a run that was not a dry run, the installer
    has opened one new window running `heading /setup-wizard` (a Linux
    desktop with x-terminal-emulator, not WSL), or `say` tells the person to
    open a new terminal and type `heading /setup-wizard`
Argument errors (exit 2) print no such line.

Examples:
  python scripts/heading-install.py --mail none --github-backup later --tz Asia/Tokyo
  python scripts/heading-install.py --target ~/work/HeadingOS --mail exchange \\
      --github-backup yes --tz Europe/Berlin
"""

from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import traceback
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.utils import install_answers  # noqa: E402 - stdlib only, imports no engine module (HOS-312)
from scripts.utils import install_lific as lific  # noqa: E402 - stdlib only, like this file (HOS-306)
from scripts.utils import install_person as person  # noqa: E402 - stdlib only (HOS-312)
from scripts.utils.install_person import WaitingForPerson  # noqa: E402 - stdlib only, the one class (HOS-312)
from scripts.utils import install_progress as progress  # noqa: E402 - stdlib only (HOS-336)

DEFAULT_FROM = "https://github.com/HEADING-OS-Official/heading-os.git"
DEFAULT_REPO_NAME = "heading-os-data"
STATE_VERSION = 1
# 22 (HOS-312): the Hindsight web console needs Node 20.9 or newer, and Node 20
# left support in April 2026, so every Node accepted or fetched here runs it.
NODE_MIN_MAJOR = 22
# The release index scripts/utils/update_sources.py already reads; newest first.
NODE_INDEX = "https://nodejs.org/dist/index.json"
PROFILE_BEGIN = "# >>> HEADING OS (written by scripts/heading-install.py) >>>"
PROFILE_END = "# <<< HEADING OS <<<"


class StepFailed(RuntimeError):
    """A step could not finish; the message is the plain line the user sees."""


# ============================================================
# Context and state
# ============================================================

@dataclass
class Ctx:
    target: Path
    source: str
    ref: str | None
    github_backup: str | None
    repo_name: str
    dry_run: bool
    mail: str | None = None
    tz: str | None = None
    proxy_signin: str | None = None
    google_client: str | None = None
    google_audience: str | None = None
    bin_dirs: list[Path] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def github(self) -> bool:
        """True when the intake answered yes to the GitHub backup."""
        return self.github_backup == "yes"

    @property
    def engine(self) -> Path:
        return self.target / "engine"

    @property
    def data(self) -> Path:
        return self.target / "data"

    @property
    def python(self) -> Path:
        return self.engine / ".venv" / "bin" / "python"

    @property
    def state_file(self) -> Path:
        return self.target / ".heading-install" / "state.json"

    def env(self) -> dict[str, str]:
        """The environment for every child: this install's data, never another's.

        Built from os.environ so the trace id and everything else is inherited.
        HEADING_OS_DATA is set only once the folder exists, because the engine
        refuses a value naming a missing directory.
        """
        env = dict(os.environ)
        path = env.get("PATH", "")
        # The engine's own environment too, for the `pre-commit` its scripts call.
        for d in [*self.bin_dirs, self.engine / ".venv" / "bin"]:
            if str(d) not in path.split(os.pathsep):
                path = f"{d}{os.pathsep}{path}"
        env["PATH"] = path
        if self.data.is_dir():
            env["HEADING_OS_DATA"] = str(self.data)
        else:
            env.pop("HEADING_OS_DATA", None)
        return env


def load_state(ctx: Ctx) -> dict:
    try:
        state = json.loads(ctx.state_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": STATE_VERSION, "steps": {}}
    except (OSError, ValueError) as exc:
        # A broken state file is only a record; every step re-checks the machine.
        print(f"  note: state file unreadable ({exc}); every step will be re-checked")
        return {"version": STATE_VERSION, "steps": {}}
    if not isinstance(state, dict) or not isinstance(state.setdefault("steps", {}), dict):
        print("  note: state file unreadable (not an object with a steps object); "
              "every step will be re-checked")
        return {"version": STATE_VERSION, "steps": {}}
    return state


def save_state(ctx: Ctx, state: dict) -> None:
    ctx.state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = ctx.state_file.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, ctx.state_file)


# ============================================================
# Helpers
# ============================================================

def run(cmd: list[str], ctx: Ctx, *, cwd: Path | None = None, capture: bool = True,
        check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=ctx.env(),
                          capture_output=capture, text=True, errors="replace")
    if check and proc.returncode != 0:
        tail = "\n".join(((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()[-15:])
        raise StepFailed(f"`{' '.join(cmd[:3])}` exited {proc.returncode}" + (f"\n{tail}" if tail else ""))
    return proc


def which(tool: str, ctx: Ctx) -> str | None:
    return shutil.which(tool, path=ctx.env()["PATH"])


def is_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8").lower()
    except OSError:
        return False


def node_home() -> Path:
    return Path.home() / ".local" / "share" / "heading-os" / "node"


def latest_release(source: str, ctx: Ctx) -> str:
    """The newest vMAJOR.MINOR.PATCH tag of `source`, the rule install.sh used.

    A dated tag (v2026.10.1, the first three releases) counts only while no
    versioned tag exists, because as a number it outranks every version.
    """
    out = run(["git", "ls-remote", "--tags", "--refs", source, "v*"], ctx).stdout
    tags = []
    for line in out.splitlines():
        m = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", line.rsplit("refs/tags/", 1)[-1].strip())
        if m:
            tags.append(tuple(int(x) for x in m.groups()))
    semver = [t for t in tags if t[0] < 1000]
    pool = semver or tags
    if not pool:
        raise StepFailed(f"{source} has no release tag (vX.Y.Z) yet; name one with --ref")
    return "v%d.%d.%d" % max(pool)


def shell_path():
    """`scripts/utils/shell_path.py`, the one home of the PATH startup-file logic (HOS-310).

    Imported from this engine checkout on first use, so the file stays
    importable before the engine's environment exists.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from scripts.utils import shell_path as module
    return module


def startup_files() -> list[Path]:
    sp = shell_path()
    return sp.startup_files(Path.home().resolve(), sp.login_shell())


def path_marker(directory: Path) -> str:
    """The line `add_to_path` writes first for `directory`, by which it finds it again."""
    sp = shell_path()
    home = Path.home().resolve()
    return sp._line(Path(directory).expanduser().resolve(), home).splitlines()[0]


def profile_block(ctx: Ctx) -> str:
    """The HEADING_OS_DATA export: the engine finds ~/HeadingOS/data only through it."""
    home = str(Path.home())
    s = str(ctx.data)
    # Single-quoted by shlex: a `$`, a backtick or `$(...)` in the target is
    # text, never something the shell runs when it reads this file.
    value = ('"$HOME"/' + shlex.quote(s[len(home) + 1:])) if s.startswith(home + "/") else shlex.quote(s)
    return "\n".join([PROFILE_BEGIN, f"export HEADING_OS_DATA={value}", PROFILE_END]) + "\n"


def with_block(text: str, block: str) -> str:
    """`text` with our block replaced in place, or appended when absent."""
    start, end = text.find(PROFILE_BEGIN), text.find(PROFILE_END)
    if start != -1 and end > start:
        return text[:start] + block + text[end + len(PROFILE_END):].lstrip("\n")
    sep = "" if not text or text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
    return text + sep + block


def compute_bin_dirs(ctx: Ctx) -> list[Path]:
    """Every directory a tool this install relies on lives in.

    ~/.local/bin always: uv, Claude Code's own installer, and the later tools
    (lific, herdr, the heading launcher) land there, and on a fresh account it
    does not exist at login, so ~/.profile never added it (measured on a
    fresh Linux Mint 22.3 account, 2026-11-01). Plus the directory of uv and claude wherever they are, and the
    Node.js this installer put in place.
    """
    dirs = [Path.home() / ".local" / "bin"]
    for tool in ("uv", "claude", "git"):
        found = which(tool, ctx)
        if found:
            d = Path(found).parent
            if str(d).startswith(str(Path.home()) + "/") and d not in dirs:
                dirs.append(d)
    node_bin = node_home() / "current" / "bin"
    if node_bin.is_dir() and node_bin not in dirs:
        dirs.append(node_bin)
    return dirs


# ============================================================
# Step registry (the extension point)
# ============================================================

@dataclass
class Step:
    name: str
    title: str
    check: Callable[[Ctx], bool]
    run: Callable[[Ctx], None]


STEPS: list[Step] = []


def step(name: str, title: str, check: Callable[[Ctx], bool]):
    """Register the decorated function as the `run` of a step, in file order."""
    def deco(fn: Callable[[Ctx], None]):
        STEPS.append(Step(name, title, check, fn))
        return fn
    return deco


# ---- Step 1: platform and prerequisites ------------------------------------

def wsl_systemd_fix() -> str:
    """`WSL_SYSTEMD_FIX` of `scripts/utils/schedule.py`, imported from this checkout on first use, as `shell_path()` is."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from scripts.utils.schedule import WSL_SYSTEMD_FIX
    return WSL_SYSTEMD_FIX


def _platform_problem(ctx: Ctx) -> str | None:
    system = platform.system()
    if system == "Linux":
        if is_wsl():
            if str(ctx.target.resolve()).startswith("/mnt/"):
                return "keep HEADING OS on the Linux filesystem (~/...), not under /mnt"
            conf = configparser.ConfigParser()
            then = "then, in Windows, `wsl --shutdown`, reopen the terminal and run this again"
            try:
                conf.read("/etc/wsl.conf", encoding="utf-8")
            except (configparser.DuplicateSectionError, configparser.DuplicateOptionError) as exc:
                if exc.section != "boot":
                    return f"/etc/wsl.conf cannot be read ({exc})"
                what = ("more than one [boot] section" if isinstance(exc, configparser.DuplicateSectionError)
                        else f"the key {exc.option} twice in [boot]")
                return (f"/etc/wsl.conf cannot be read: it holds {what} ({exc}). "
                        f"Repair it, keeping one [boot] with systemd=true: {wsl_systemd_fix()} {then}")
            except configparser.Error as exc:
                return f"/etc/wsl.conf cannot be read ({exc})"
            if conf.get("boot", "systemd", fallback="").strip().lower() != "true":
                return ("WSL runs without systemd, which the background services need. Run: "
                        f"{wsl_systemd_fix()} {then}")
    elif system == "Darwin":
        if subprocess.run(["xcode-select", "-p"], capture_output=True).returncode != 0:
            return "the Command Line Tools are missing. Run: xcode-select --install, then run this again"
    else:
        return f"unsupported system {system}. Use Linux, macOS, or WSL2 on Windows"
    return None


def _node_ok(ctx: Ctx) -> bool:
    node = which("node", ctx)
    if not node:
        return False
    out = subprocess.run([node, "--version"], capture_output=True, text=True).stdout.strip()
    m = re.match(r"v(\d+)", out)
    return bool(m) and int(m.group(1)) >= NODE_MIN_MAJOR


def _python311_ok(ctx: Ctx) -> bool:
    uv = which("uv", ctx)
    return bool(uv) and run([uv, "python", "find", "3.11"], ctx, check=False).returncode == 0


def check_prereqs(ctx: Ctx) -> bool:
    return (_platform_problem(ctx) is None and bool(which("git", ctx)) and bool(which("uv", ctx))
            and _python311_ok(ctx) and _node_ok(ctx))


def install_node(ctx: Ctx) -> None:
    """Node.js from nodejs.org into ~/.local/share/heading-os/node, checksum-verified.

    No sudo and no package manager: the npx-based MCP bridges need only a node
    and npm on PATH, which the profile block provides.
    """
    machine = platform.machine().lower()
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    osname = {"Linux": "linux", "Darwin": "darwin"}[platform.system()]
    if not arch:
        raise StepFailed(f"no Node.js build for this processor ({machine}); install Node.js {NODE_MIN_MAJOR}+ yourself")
    if not hasattr(tarfile, "data_filter"):
        raise StepFailed("this Python cannot unpack archives safely; install Node.js yourself")
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from scripts.utils.user_agent import user_agent  # stdlib only; the one workspace constant

    def fetch(url: str, timeout: int):
        if not url.startswith("https://"):
            raise StepFailed(f"refusing a non-https download: {url}")
        req = urllib.request.Request(url, headers={"User-Agent": user_agent()})  # noqa: S310 - https only, checked above
        return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 - https only, checked above

    with fetch(NODE_INDEX, 60) as resp:
        index = json.loads(resp.read().decode("utf-8"))
    version = next((e["version"] for e in index if isinstance(e, dict) and e.get("lts")), None)
    if not version:
        raise StepFailed("the Node.js release index lists no LTS release")
    dist = NODE_INDEX.rsplit("/", 1)[0] + f"/{version}/"
    with fetch(dist + "SHASUMS256.txt", 60) as resp:
        sums = resp.read().decode("utf-8")
    suffix = ".tar.xz" if osname == "linux" else ".tar.gz"
    m = re.search(rf"^([0-9a-f]{{64}})\s+(node-v[\d.]+-{osname}-{arch}{re.escape(suffix)})$", sums, re.M)
    if not m:
        raise StepFailed(f"nodejs.org lists no {osname}-{arch} archive")
    digest, name = m.groups()
    home = node_home()
    home.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=home) as tmp:
        archive = Path(tmp) / name
        with fetch(dist + name, 300) as resp, archive.open("wb") as out:
            shutil.copyfileobj(resp, out)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
            raise StepFailed(f"{name} does not match its published checksum; nothing was installed")
        with tarfile.open(archive) as tar:
            tar.extractall(tmp, filter="data")
        unpacked = Path(tmp) / name[: -len(suffix)]
        current = home / "current"
        if current.exists():
            shutil.rmtree(current)
        os.replace(unpacked, current)
    ctx.bin_dirs = compute_bin_dirs(ctx)
    probe = subprocess.run([str(current / "bin" / "node"), "--version"], capture_output=True, text=True)
    if probe.returncode != 0:
        lib = re.search(r"loading shared libraries: (\S+?):", probe.stderr)
        if lib:
            hint = " (Ubuntu, Mint, Debian: sudo apt-get install -y libatomic1)" if lib.group(1) == "libatomic.so.1" else ""
            raise StepFailed(f"Node.js {version} needs the system library {lib.group(1)}. "
                             f"Install it{hint}, then run this again")
        raise StepFailed(f"Node.js {version} was unpacked but does not start: {probe.stderr.strip()[-200:]}")


@step("prerequisites", "Platform and prerequisites (git, uv, Python 3.11, Node.js)", check_prereqs)
def run_prereqs(ctx: Ctx) -> None:
    problem = _platform_problem(ctx)
    if problem:
        raise StepFailed(problem)
    if not which("git", ctx):
        raise StepFailed("git is not installed; run install.sh, which installs it, or your package manager")
    uv = which("uv", ctx)
    if not uv:
        raise StepFailed("uv is not installed; run install.sh, which installs it")
    if not _python311_ok(ctx):
        run([uv, "python", "install", "3.11"], ctx)
    if not _node_ok(ctx):
        install_node(ctx)
    if platform.system() == "Linux" and not which("zstd", ctx):
        # A hint, not a prerequisite: only the local index step needs it (HOS-305).
        print("  note: the local search step will need zstd: sudo apt-get install -y zstd")


# ---- Step 2: folders --------------------------------------------------------

@step("folders", "Folders engine/ and data/", lambda c: c.target.is_dir() and
      (c.engine.is_dir() or not c.engine.exists()) and c.data.is_dir())
def run_folders(ctx: Ctx) -> None:
    if ctx.engine.exists() and not ctx.engine.is_dir():
        raise StepFailed(f"{ctx.engine} exists and is not a folder; move it aside, then run this again")
    if ctx.data.exists() and not ctx.data.is_dir():
        raise StepFailed(f"{ctx.data} exists and is not a folder; move it aside, then run this again")
    ctx.data.mkdir(parents=True, exist_ok=True)


# ---- Step 3: engine ---------------------------------------------------------

def check_engine(ctx: Ctx) -> bool:
    return (ctx.engine / ".git").exists() and (ctx.engine / "scripts" / "create-data-repo.py").is_file()


@step("engine", "Engine, cloned at the latest release", check_engine)
def run_engine(ctx: Ctx) -> None:
    if ctx.engine.exists() and any(ctx.engine.iterdir()):
        if not (ctx.engine / ".git").exists():
            raise StepFailed(f"{ctx.engine} is not empty and is not the engine; move it aside, then run this again")
        raise StepFailed(f"{ctx.engine} is a git clone but not a complete engine; move it aside, then run this again")
    ref = ctx.ref or latest_release(ctx.source, ctx)
    if ctx.engine.exists():
        ctx.engine.rmdir()  # empty: git clone wants to create it
    # Cloned AT the tag. Never a clone of the default branch and a checkout
    # afterwards: a checkout can replace the very installer that is running.
    run(["git", "-c", "advice.detachedHead=false", "clone", "--quiet", "--branch", ref, ctx.source, str(ctx.engine)], ctx)
    ctx.notes.append(f"engine {ref}")


# ---- Step 4: Python environment ---------------------------------------------

def check_pyenv(ctx: Ctx) -> bool:
    """Every locked package of every extra is installed, not only one marker.

    Asks uv itself: `uv sync --all-extras --inexact --dry-run` prints
    "Would make no changes" when the environment already holds the whole
    lock, and "Would install ..." otherwise (MEASURED 2026-11-01, uv 0.13.5;
    the text is uv's, not ours, so a uv that words it differently reads as
    "not in place" and the step re-runs, which is the safe direction).
    `--inexact` so packages the operator added are not counted as drift.
    """
    if not ctx.python.exists():
        return False
    proc = run([which("uv", ctx) or "uv", "sync", "--all-extras", "--inexact", "--python", "3.11",
                "--dry-run"], ctx, cwd=ctx.engine, check=False)
    return proc.returncode == 0 and "Would make no changes" in (proc.stdout + proc.stderr)


@step("python-env", "Python environment with every module's libraries", check_pyenv)
def run_pyenv(ctx: Ctx) -> None:
    # Every extra: every module is mandatory, and the yards already run the
    # full set (`uv sync --all-extras`), so HELM and an executive match.
    run([which("uv", ctx) or "uv", "sync", "--quiet", "--all-extras", "--python", "3.11"], ctx, cwd=ctx.engine)
    if not check_pyenv(ctx):
        raise StepFailed("some libraries did not install; run: uv sync --all-extras, in the engine folder")


# ---- Step 5: data repository ------------------------------------------------

def _data_is_repo(ctx: Ctx) -> bool:
    return (ctx.data / ".schema-version").is_file() and (ctx.data / ".git").exists()


def _has_origin(ctx: Ctx) -> bool:
    return run(["git", "-C", str(ctx.data), "remote", "get-url", "origin"], ctx, check=False).returncode == 0


def _origin_slug(ctx: Ctx) -> str | None:
    """`owner/name` of origin when it is a GitHub repository, else None.

    Every fetch URL and every push URL must name the same repository: `git push
    origin` sends to all of them, so a second url or pushurl, or a
    pushInsteadOf, would otherwise send the data somewhere this check never saw
    (`--all`: without it git prints only the first, MEASURED 2026-11-01 by a
    reproduction run on throwaway repositories, HOS-301 review round 3).
    """
    slugs = set()
    for extra in ([], ["--push"]):
        proc = run(["git", "-C", str(ctx.data), "remote", "get-url", "--all", *extra, "origin"], ctx, check=False)
        urls = proc.stdout.split() if proc.returncode == 0 else []
        for url in urls or [""]:
            m = re.fullmatch(r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^/\s]+/[^/\s]+?)(?:\.git)?/?",
                             url)
            slugs.add(m.group(1) if m else None)
    return slugs.pop() if len(slugs) == 1 else None


def _origin_is_private(ctx: Ctx) -> bool:
    """True only when GitHub itself answers that origin is private. Asked on every run."""
    gh, slug = which("gh", ctx), _origin_slug(ctx)
    if not gh or not slug:
        return False
    view = run([gh, "repo", "view", slug, "--json", "isPrivate", "--jq", ".isPrivate"], ctx, check=False)
    return view.returncode == 0 and view.stdout.strip() == "true"


def _origin_matches(ctx: Ctx) -> bool:
    """True when origin's main is exactly the local main: the first push landed."""
    local = run(["git", "-C", str(ctx.data), "rev-parse", "--verify", "refs/heads/main"], ctx, check=False)
    remote = run(["git", "-C", str(ctx.data), "ls-remote", "origin", "refs/heads/main"], ctx, check=False)
    return (local.returncode == 0 and remote.returncode == 0
            and remote.stdout.split("\t", 1)[0].strip() == local.stdout.strip() != "")


def check_data(ctx: Ctx) -> bool:
    if not _data_is_repo(ctx):
        return False
    if not ctx.github:
        return True
    return _has_origin(ctx) and _origin_is_private(ctx) and _origin_matches(ctx)


def _require_private_origin(ctx: Ctx) -> None:
    if not _origin_is_private(ctx):
        slug = _origin_slug(ctx) or "origin"
        raise StepFailed(f"{slug} is not a private GitHub repository (or GitHub did not confirm it); "
                         "your data never goes there. Make it private, or remove that origin")


def _create_data(ctx: Ctx, *extra: str) -> None:
    run([str(ctx.python), str(ctx.engine / "scripts" / "create-data-repo.py"),
         "--path", str(ctx.data), "--repo-name", ctx.repo_name, *extra], ctx, cwd=ctx.engine)


GITHUB_SIGNIN = "signin-github"
GITHUB_NO_CODE = "The GitHub sign-in started but printed no code yet. Run the installer again in a moment."
SIGNIN_WAIT_S = 15.0
# gh 2.101.0 prints "First copy your one-time code: %s" and "%s to open %s in
# your browser... " (format strings read from the binary, 2026-11-03). Whether
# the address it prints carries https:// was not measured, so either form counts.
_GH_CODE = re.compile(r"one-time code: ([A-Z0-9]{4}-[A-Z0-9]{4})")
_GH_ADDRESS = re.compile(r"(?:https://)?github\.com\S*")


def _github_say(ctx: Ctx) -> str | None:
    """The sentence to relay once gh printed its code and address, else None."""
    tail = person.log_tail(ctx, GITHUB_SIGNIN)
    code = _GH_CODE.search(tail)
    if not code or not _GH_ADDRESS.search(tail):
        return None
    return ("GitHub needs you to sign in. Your browser opens github.com/login/device; enter the code "
            f"{code.group(1)} there. If no page opened, open https://github.com/login/device yourself. "
            "Then tell me you are done.")


def _github_sign_in(ctx: Ctx, gh: str) -> None:
    """Start `gh auth login --web` detached and stop for the person; never returns."""
    if person.running(ctx, GITHUB_SIGNIN):
        raise WaitingForPerson(_github_say(ctx) or GITHUB_NO_CODE)
    if person.ended(ctx, GITHUB_SIGNIN):
        person.clear(ctx, GITHUB_SIGNIN)
        raise StepFailed("the GitHub sign-in ended without signing in. Run the installer again to start a new one.")
    person.start_detached(ctx, GITHUB_SIGNIN,
                          [gh, "auth", "login", "--hostname", "github.com", "--git-protocol", "https", "--web"],
                          env=ctx.env(), cwd=ctx.target)
    deadline = time.monotonic() + SIGNIN_WAIT_S
    while _github_say(ctx) is None and time.monotonic() < deadline:
        time.sleep(0.5)
    raise WaitingForPerson(_github_say(ctx) or GITHUB_NO_CODE)


def _github(ctx: Ctx) -> None:
    gh = which("gh", ctx)
    if not gh:
        raise StepFailed("the GitHub command line tool (gh) is not installed: https://cli.github.com, "
                         "or run without --github to keep the data local")
    if run([gh, "auth", "status"], ctx, check=False).returncode != 0:
        _github_sign_in(ctx, gh)
    person.clear(ctx, GITHUB_SIGNIN)
    run([gh, "auth", "setup-git"], ctx)
    if _data_is_repo(ctx) and _has_origin(ctx):
        _require_private_origin(ctx)
        if not _origin_matches(ctx):
            _create_data(ctx)  # origin is set, so it only pushes, verified
        return
    owner = run([gh, "api", "user", "--jq", ".login"], ctx).stdout.strip()
    slug = f"{owner}/{ctx.repo_name}"
    view = run([gh, "repo", "view", slug, "--json", "isPrivate,url", "--jq", "[.isPrivate, .url] | @tsv"],
               ctx, check=False)
    if view.returncode != 0:
        _create_data(ctx)  # creates the private repository and pushes, verified
        return
    is_private, url = (view.stdout.strip().split("\t") + [""])[:2]
    if is_private != "true":
        raise StepFailed(f"{slug} exists and is PUBLIC; your data never goes there. "
                         "Make it private on GitHub, or choose another name with --repo-name")
    remote_empty = run(["git", "ls-remote", url], ctx).stdout.strip() == ""
    if not _data_is_repo(ctx):
        if remote_empty:
            _create_data(ctx, "--no-remote")
        else:
            # Adopted: the data already lives there (a second machine, a reinstall).
            run(["git", "clone", "--quiet", url + ".git", str(ctx.data)], ctx)
            if not (ctx.data / ".schema-version").is_file():
                raise StepFailed(f"{slug} is not a HEADING OS data repository; choose another name with --repo-name")
            return
    if _has_origin(ctx):
        # Whatever origin is, on every run: a repository made public later,
        # or an origin set by hand, is refused before anything is pushed.
        _require_private_origin(ctx)
        if not _origin_matches(ctx):
            _create_data(ctx)  # origin is set, so it only pushes, verified
        return
    if not remote_empty:
        raise StepFailed(f"{slug} already holds data and {ctx.data} holds other data; "
                         "the two cannot be joined automatically")
    run(["git", "-C", str(ctx.data), "remote", "add", "origin", url + ".git"], ctx)
    _create_data(ctx)  # origin is set, so it only pushes, verified


@step("data", "Your private data repository", check_data)
def run_data(ctx: Ctx) -> None:
    if not _data_is_repo(ctx) and any(ctx.data.iterdir()):
        raise StepFailed(f"{ctx.data} is not empty and is not a HEADING OS data repository; "
                         "move it aside, then run this again")
    if ctx.github:
        _github(ctx)
    else:
        _create_data(ctx, "--no-remote")
    if not check_data(ctx):
        raise StepFailed("the data repository is still incomplete")


def _layout_cmd(ctx: Ctx, option: str) -> list[str]:
    return [str(ctx.python), str(ctx.engine / "scripts" / "create-data-repo.py"),
            "--path", str(ctx.data), option]


def check_data_layout(ctx: Ctx) -> bool:
    # HOS-321, HOS-322: a data repository from an older installer has README.md
    # at its root and no thread homes, and both stop the engine push.
    return (ctx.python.is_file()
            and (ctx.engine / "scripts" / "create-data-repo.py").is_file()
            and _data_is_repo(ctx)
            and run(_layout_cmd(ctx, "--check-layout"), ctx, cwd=ctx.engine, check=False).returncode == 0)


@step("data-layout", "Your data repository's layout, as the push checks expect it", check_data_layout)
def run_data_layout(ctx: Ctx) -> None:
    # Never pushes: a --github install's data step sends it on the next rerun.
    run(_layout_cmd(ctx, "--repair-layout"), ctx, cwd=ctx.engine)
    print("  the repair is committed in your data repository; your next backup sends it")


# ---- Step 6: settings and gates ---------------------------------------------

def check_gates(ctx: Ctx) -> bool:
    py, s = str(ctx.python), ctx.engine / "scripts"
    return ((ctx.engine / ".env").is_file()
            and run([py, str(s / "install-hooks.py"), "--check"], ctx, cwd=ctx.engine, check=False).returncode == 0
            and run([py, str(s / "install-git-hooks.py"), "--check"], ctx, cwd=ctx.engine, check=False).returncode == 0
            and run(["bash", str(s / "setup-platform.sh"), "--check"], ctx, cwd=ctx.engine, check=False).returncode == 0)


@step("gates", "Settings file and safety checks", check_gates)
def run_gates(ctx: Ctx) -> None:
    env_file = ctx.engine / ".env"
    if not env_file.exists():
        shutil.copyfile(ctx.engine / ".env.example", env_file)
    os.chmod(env_file, 0o600)
    run([str(ctx.engine / ".venv" / "bin" / "pre-commit"), "install"], ctx, cwd=ctx.engine)
    run([str(ctx.python), "scripts/install-git-hooks.py"], ctx, cwd=ctx.engine)
    run(["bash", "scripts/setup-platform.sh"], ctx, cwd=ctx.engine)
    if not check_gates(ctx):
        raise StepFailed("a safety check is still not armed; run: bash scripts/setup-platform.sh --check")


# ---- HOS-312: the intake's answers, kept in .env ----------------------------
# HEADING_OS_GITHUB_BACKUP and HEADING_OS_PROXY_SIGNIN here; HEADING_OS_TZ and HEADING_OS_MAIL by the
# timezone and mail-choice steps below. Read back through install_answers.

from scripts.utils.env_file import upsert_env_value  # noqa: E402 - the one .env writer


def check_answers(ctx: Ctx) -> bool:
    return (install_answers.github_backup(ctx.engine, environ=False) == ctx.github_backup
            and install_answers.proxy_signin(ctx.engine, environ=False) == ctx.proxy_signin)


@step("answers", "Your install answers, kept in the settings file", check_answers)
def run_answers(ctx: Ctx) -> None:
    for name, value in ((install_answers.GITHUB_BACKUP_ENV, ctx.github_backup),
                        (install_answers.PROXY_SIGNIN_ENV, ctx.proxy_signin)):
        try:
            upsert_env_value(ctx.engine / ".env", name, value)
        except ValueError as exc:
            raise StepFailed(f"{name} could not be written to .env: {exc}") from exc


# ---- Step 7: shell startup files --------------------------------------------

def check_shell(ctx: Ctx) -> bool:
    block = profile_block(ctx)
    markers = [path_marker(d) for d in ctx.bin_dirs]
    for f in startup_files():
        text = f.read_text(encoding="utf-8", errors="replace") if f.is_file() else ""
        lines = text.splitlines()
        if block not in text or any(m not in lines for m in markers):
            return False
    return True


@step("shell", "Tools and data found in every new terminal", check_shell)
def run_shell(ctx: Ctx) -> None:
    sp = shell_path()
    for d in ctx.bin_dirs:
        sp.add_to_path(d)
    block = profile_block(ctx)
    for f in startup_files():
        old = f.read_text(encoding="utf-8", errors="replace") if f.exists() else ""
        new = with_block(old, block)
        if new == old:
            continue
        tmp = f.with_name(f.name + ".heading-install.tmp")
        tmp.write_text(new, encoding="utf-8")
        if f.exists():
            os.chmod(tmp, f.stat().st_mode & 0o777)
        os.replace(tmp, f)


# ---- HOS-307: Herdr and CLIProxyAPI -----------------------------------------
# The logic is scripts/utils/install_herdr_cliproxy.py; each check reads the
# real state, and a failure there is an OSError this loop reports as a step.

from scripts.utils import install_herdr_cliproxy as hc  # noqa: E402


@step("herdr", "Herdr, the terminal that runs parallel yards", hc.herdr_ready)
def run_herdr(ctx: Ctx) -> None:
    hc.install_herdr(ctx)


@step("herdr-plugin", "Herdr's yard plugin and its view of Claude Code", hc.herdr_plugin_ready)
def run_herdr_plugin(ctx: Ctx) -> None:
    hc.install_herdr_plugin(ctx)


@step("cliproxy", "CLIProxyAPI, the local door to other AI models", hc.cliproxy_ready)
def run_cliproxy(ctx: Ctx) -> None:
    hc.install_cliproxy(ctx)


@step("cliproxy-config", "CLIProxyAPI settings and its private key", hc.cliproxy_config_ready)
def run_cliproxy_config(ctx: Ctx) -> None:
    hc.install_cliproxy_config(ctx)


@step("cliproxy-service", "CLIProxyAPI running in the background", hc.cliproxy_service_ready)
def run_cliproxy_service(ctx: Ctx) -> None:
    hc.install_cliproxy_service(ctx)


@step("cliproxy-signin", "Model sign-ins: Claude, then the others you choose", hc.signin_ready)
def run_cliproxy_signin(ctx: Ctx) -> None:
    hc.install_signins(ctx)


# ---- HOS-305: local semantic index ------------------------------------------
# ollama and bge-m3 in user space, the first build, the hourly refresh; the
# logic is in scripts/utils/install_index.py, standard library only.

from scripts.utils import install_index as _index  # noqa: E402


@step("embedding-server", "Local search engine (ollama, a large download that resumes if interrupted)",
      _index.check_server)
def run_embedding_server(ctx: Ctx) -> None:
    try:
        _index.install_server(ctx)
    except _index.InstallError as exc:
        raise StepFailed(str(exc)) from exc


@step("embedding-model", "Search language model bge-m3 (about 1.2 GB, resumes if interrupted)",
      _index.check_model)
def run_embedding_model(ctx: Ctx) -> None:
    try:
        _index.pull_model(ctx)
    except _index.InstallError as exc:
        raise StepFailed(str(exc)) from exc


@step("index-build", "First search index of your engine and data, refreshed every hour", _index.check_index)
def run_index_build(ctx: Ctx) -> None:
    try:
        _index.build_index(ctx)
    except _index.InstallError as exc:
        raise StepFailed(str(exc)) from exc


# ---- HOS-306: Lific, the issue tracker --------------------------------------
# The logic is in scripts/utils/install_lific.py; a LificError is the plain line.

def _lific(fn: Callable[[Ctx], None], ctx: Ctx) -> None:
    try:
        fn(ctx)
    except lific.LificError as exc:
        raise StepFailed(str(exc)) from exc


@step("lific-binary", f"Lific {lific.VERSION}, the issue tracker, checksum-verified", lific.check_binary)
def run_lific_binary(ctx: Ctx) -> None:
    _lific(lific.run_binary, ctx)


@step("lific-init", "Lific's settings and database, reachable only from this computer", lific.check_init)
def run_lific_init(ctx: Ctx) -> None:
    _lific(lific.run_init, ctx)


@step("lific-service", "Lific's background service, running and answering", lific.check_service)
def run_lific_service(ctx: Ctx) -> None:
    _lific(lific.run_service, ctx)


@step("lific-project", "Lific project HOS with Claude's own account and key", lific.check_project)
def run_lific_project(ctx: Ctx) -> None:
    _lific(lific.run_project, ctx)


@step("lific-mcp", "Lific connected to Claude Code", lific.check_mcp)
def run_lific_mcp(ctx: Ctx) -> None:
    _lific(lific.run_mcp, ctx)


# ---- HOS-304: Hindsight -----------------------------------------------------
# The memory server on this computer, its key in every place, Claude Code and
# the timers wired; the logic is in scripts/utils/install_hindsight.py, and a
# HindsightError is an OSError this loop reports as a failed step.

from scripts.utils import install_hindsight as hs  # noqa: E402


@step("hindsight-server", "Hindsight, the memory server, on this computer only", hs.check_server_install)
def run_hindsight_server(ctx: Ctx) -> None:
    hs.run_server_install(ctx)


@step("hindsight-settings", "Hindsight's settings and its private key", hs.check_settings)
def run_hindsight_settings(ctx: Ctx) -> None:
    hs.run_settings(ctx)


@step("hindsight-service", "Hindsight running in the background", hs.check_service)
def run_hindsight_service(ctx: Ctx) -> None:
    hs.run_service(ctx)


from scripts.utils import install_hindsight_console as hs_console  # noqa: E402 - HOS-312


@step("hindsight-console", "The Hindsight web console, on this computer only", hs_console.check_install)
def run_hindsight_console(ctx: Ctx) -> None:
    hs_console.run_install(ctx)


@step("hindsight-console-service", "The Hindsight web console running in the background", hs_console.check_service)
def run_hindsight_console_service(ctx: Ctx) -> None:
    hs_console.run_service(ctx)


@step("hindsight-bank", "Your memory bank, main", hs.check_bank)
def run_hindsight_bank(ctx: Ctx) -> None:
    hs.run_bank(ctx)


@step("hindsight-claude", "Claude Code connected to your memory", hs.check_claude)
def run_hindsight_claude(ctx: Ctx) -> None:
    hs.run_claude(ctx)


@step("hindsight-timers", "Memory kept current: sync and snapshot", hs.check_timers)
def run_hindsight_timers(ctx: Ctx) -> None:
    hs.run_timers(ctx)


# ---- HOS-308: AgentView and the Claude Code plugins ----
# The logic is in scripts/utils/install_agentview.py; an AgentViewError is the plain line.

from scripts.utils import install_agentview as agentview  # noqa: E402 - stdlib only, like this file


def _agentview(fn: Callable[[Ctx], None], ctx: Ctx) -> None:
    try:
        fn(ctx)
    except agentview.AgentViewError as exc:
        raise StepFailed(str(exc)) from exc


@step("agent-watch", "The agent-watch panel for subagents", agentview.check_agent_watch)
def run_agent_watch(ctx: Ctx) -> None:
    _agentview(agentview.run_agent_watch, ctx)


@step("plugins", "Claude Code plugins the engine uses", agentview.check_plugins)
def run_plugins(ctx: Ctx) -> None:
    _agentview(agentview.run_plugins, ctx)


@step("agentsview", "AgentsView, the archive of your past sessions", agentview.check_agentsview)
def run_agentsview(ctx: Ctx) -> None:
    _agentview(agentview.run_agentsview, ctx)


@step("usage-dashboard", "The proxy usage dashboard, refreshed on a timer", agentview.check_usage_dashboard)
def run_usage_dashboard(ctx: Ctx) -> None:
    _agentview(agentview.run_usage_dashboard, ctx)


# ---- HOS-309: mail, calendar and timezone -----------------------------------
# The logic is scripts/utils/install_mail.py, and for Google
# scripts/utils/install_google.py (HOS-324). With --mail exchange, a first
# install stops at mail-account (exit 1) until the executive has set the three
# Exchange names with heading-secrets.py; that stop is expected. With --mail
# google, it stops at each Google step until the executive has done that step
# in their own Google console or terminal; each stop names the command.

from scripts.utils import install_google as _google  # noqa: E402
from scripts.utils import install_mail as _mail  # noqa: E402


@step("timezone", "Your time zone, for every scheduled job", _mail.timezone_ready)
def run_timezone(ctx: Ctx) -> None:
    _mail.install_timezone(ctx)


@step("mail-choice", "Which mail system HEADING OS reads", _mail.mail_choice_ready)
def run_mail_choice(ctx: Ctx) -> None:
    _mail.install_mail_choice(ctx)


@step("google-client", "Your own Google sign-in app (the client file)", _google.google_client_ready)
def run_google_client(ctx: Ctx) -> None:
    _google.install_google_client(ctx)


@step("google-audience", "Your Google app published (In production, or Internal)", _google.google_audience_ready)
def run_google_audience(ctx: Ctx) -> None:
    _google.install_google_audience(ctx)


@step("google-consent", "Gmail and Google Calendar signed in", _google.google_consent_ready)
def run_google_consent(ctx: Ctx) -> None:
    _google.install_google_consent(ctx)


@step("mail-account", "Your Exchange mailbox and calendar", _mail.mail_account_ready)
def run_mail_account(ctx: Ctx) -> None:
    _mail.install_mail_account(ctx)


@step("mail-services", "Mail sync and Sentinel running in the background", _mail.mail_services_ready)
def run_mail_services(ctx: Ctx) -> None:
    _mail.install_mail_services(ctx)


# ---- Step 8: what a new terminal finds --------------------------------------

def _fresh_login_finds(ctx: Ctx) -> tuple[bool, str]:
    """Ask a NEW login shell, with this process's HEADING_OS_DATA removed, where the data is.

    The `env -u HEADING_OS_DATA` check install.sh made, now through the startup
    files a new terminal reads, because the engine finds ~/HeadingOS/data only
    through HEADING_OS_DATA (its folder is not named .heading-os-data).
    """
    code = "from scripts.utils.paths import get_data_root; print(get_data_root())"
    shell = shell_path().login_shell()
    shell = shell if shell in ("bash", "zsh") else "bash"
    proc = subprocess.run(
        ["env", "-u", "HEADING_OS_DATA", shell, "-lc",
         f"cd {shlex.quote(str(ctx.engine))} && command -v uv >/dev/null && "
         f"{shlex.quote(str(ctx.python))} -c {shlex.quote(code)}"],
        capture_output=True, text=True, errors="replace")
    found = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    try:
        ok = proc.returncode == 0 and Path(found).resolve() == ctx.data.resolve()
    except OSError:
        ok = False
    return ok, found or proc.stderr.strip()[-300:]


# ---- Every mandatory service family, through the service layer ----
# `install --all` and `status --all` both take select_families(mandatory_only=True)
# (scripts/heading-services.py), so the check asks about exactly what the run installs.

def _services_all(ctx: Ctx, verb: str, capture: bool) -> subprocess.CompletedProcess:
    return run([str(ctx.python), str(ctx.engine / "scripts" / "heading-services.py"), verb, "--all"],
               ctx, cwd=ctx.engine, capture=capture, check=False)


def check_services(ctx: Ctx) -> bool:
    if ctx.dry_run or not ctx.python.exists():
        return False  # its check asks the service manager, and a dry run asks nothing
    return _services_all(ctx, "status", capture=True).returncode == 0


@step("services", "Every background service HEADING OS needs", check_services)
def run_services(ctx: Ctx) -> None:
    if _services_all(ctx, "install", capture=False).returncode != 0:
        raise StepFailed("the service layer could not install every service; "
                         "see the lines above (scripts/heading-services.py install --all)")


# ---- The heading command (HOS-312) ----
# scripts/utils/heading_launcher.py: the yard refusal stays in its CLI main(),
# so this step runs in a scratch HOME too.

def check_launcher(ctx: Ctx) -> bool:
    if ctx.dry_run:
        return False  # a dry run asks nothing
    from scripts.utils.heading_launcher import launcher_ready
    return launcher_ready(ctx.engine)


@step("launcher", "The heading command, which opens Claude in the HEADING OS folder", check_launcher)
def run_launcher(ctx: Ctx) -> None:
    from scripts.utils.heading_launcher import install_launcher
    print(install_launcher(ctx.engine).message)


# ---- The admin console page (HOS-333) ----

def console_page_via_cli(python: Path, engine: Path, data_root: Path) -> Path | None:
    """`index.html` in `runtime:admin-console/cache` of `data_root`, asked of its registry; None when not answered.

    Asked through scripts/data-path.py as `install_hindsight.ledger_path` asks,
    because this file imports no engine module that reads the registry.
    """
    try:
        proc = subprocess.run([str(python), str(engine / "scripts" / "data-path.py"), "--data-root", str(data_root),
                               "path", "runtime:admin-console/cache"],
                              cwd=str(engine), capture_output=True, text=True, errors="replace", timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None  # reported by the caller as "its folder could not be found"
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return Path(proc.stdout.strip().splitlines()[-1]) / "index.html"


def check_admin_console(ctx: Ctx) -> bool:
    if ctx.dry_run or not ctx.python.exists():
        return False  # a dry run asks nothing
    page = console_page_via_cli(ctx.python, ctx.engine, ctx.data)
    registry = ctx.engine / "scripts" / "utils" / "web_interfaces.py"
    try:
        return page is not None and page.stat().st_mtime > registry.stat().st_mtime
    except OSError:
        return False


@step("admin-console", "The admin console page, with every web interface and the guides", check_admin_console)
def run_admin_console(ctx: Ctx) -> None:
    run([str(ctx.python), str(ctx.engine / "scripts" / "heading-console.py"), "build", "--engine", str(ctx.engine)],
        ctx, cwd=ctx.engine)


@step("verify", "A new terminal finds the engine's tools and your data", lambda c: _fresh_login_finds(c)[0])
def run_verify(ctx: Ctx) -> None:
    ok, found = _fresh_login_finds(ctx)
    if not ok:
        raise StepFailed(f"a new terminal resolves the data at {found!r}, not {ctx.data}")


# ---- The last check: doctor (HOS-312) ----
# Last, after `verify`, so `done` and DONE_SAY ("every check is green") follow
# only from doctor exiting 0. A warn row does not fail it; a red row does.

def _doctor(ctx: Ctx) -> tuple[int, list[str], str]:
    """`scripts/doctor.py --json` of the installed engine: (exit code, red rows as "component: check", tail).

    The install's own environment (ctx.env()), without an inherited
    WORKSPACE_ROOT, which would repoint doctor at another tree. Read-only, so
    running it again changes nothing.
    """
    env = ctx.env()
    env.pop("WORKSPACE_ROOT", None)
    proc = subprocess.run([str(ctx.python), str(ctx.engine / "scripts" / "doctor.py"), "--json"],
                          cwd=str(ctx.engine), env=env, capture_output=True, text=True, errors="replace")
    tail = "\n".join(((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()[-15:])
    try:
        rows = json.loads(proc.stdout).get("rows") or []
    except (ValueError, AttributeError):
        rows = []  # no parsable rows: the caller reports the exit code and the tail instead
    red = [f"{r.get('component')}: {r.get('check')} ({r.get('detail')})"
           for r in rows if isinstance(r, dict) and r.get("status") == "red"]
    return proc.returncode, red, tail


def check_doctor(ctx: Ctx) -> bool:
    if ctx.dry_run or not ctx.python.exists():
        return False  # a dry run asks nothing
    return _doctor(ctx)[0] == 0


@step("doctor", "Every HEADING OS check is green (scripts/doctor.py)", check_doctor)
def run_doctor(ctx: Ctx) -> None:
    rc, red, tail = _doctor(ctx)
    if rc == 0:
        return
    if red:
        raise StepFailed(f"doctor found {len(red)} red: " + "; ".join(red)
                         + ". Each row's fix is printed by: .venv/bin/python scripts/doctor.py")
    raise StepFailed(f"doctor exited {rc}" + (f"\n{tail}" if tail else ""))


# ============================================================
# Main
# ============================================================

def refresh_hint(ctx: Ctx) -> str | None:
    """The one command that gives THIS terminal what new terminals now get, if needed."""
    path = os.environ.get("PATH", "").split(os.pathsep)
    stale = any(str(d) not in path for d in ctx.bin_dirs) or os.environ.get("HEADING_OS_DATA") != str(ctx.data)
    if not stale:
        return None
    return "source ~/.zshrc" if shell_path().login_shell() == "zsh" else "source ~/.bashrc"


def print_web_interfaces(ctx: Ctx) -> None:
    """The closing list of every web interface (HOS-312), from the one list in scripts/utils/web_interfaces.py."""
    from scripts.utils.web_interfaces import answers, web_interfaces

    page = console_page_via_cli(ctx.python, ctx.engine, ctx.data)
    if page is None:
        print("\nAdmin console: its folder could not be found "
              "(run: .venv/bin/python scripts/data-path.py path runtime:admin-console/cache)")
    elif not page.is_file():
        print("\nAdmin console: not built (run: .venv/bin/python scripts/heading-console.py build)")
    else:
        print(f"\nAdmin console: {page}")
        if is_wsl():
            try:
                proc = subprocess.run(["wslpath", "-w", str(page)], capture_output=True, text=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired) as exc:
                print(f"  In Windows: not available ({exc})")
            else:
                win = proc.stdout.strip()
                print(f"  In Windows: {win}" if proc.returncode == 0 and win else
                      f"  In Windows: not available ({(proc.stderr.strip() or f'wslpath exited {proc.returncode}')[:200]})")
    print("\nWeb interfaces on this computer:")
    for wi in web_interfaces(ctx.engine):
        if not wi.url:
            print(f"  {wi.name}: not set up")
        elif answers(wi.url):
            print(f"  {wi.name}: {wi.url}" + (f" ({wi.sign_in})" if wi.sign_in else ""))
        else:
            print(f"  {wi.name}: not answering yet ({wi.url}" + (f"; {wi.sign_in})" if wi.sign_in else ")"))


# ---- HOS-312: the handoff to /setup-wizard (design § 3.6) -----------------

DONE_SAY = "HEADING OS is installed and every check is green."


HANDOFF_CLOSE = ("The terminal where the install ran is no longer needed and can be closed; "
                 "the setup continues in the new window.")
HANDOFF_HERDR = (f"{DONE_SAY} A new terminal window is opening with Herdr and Claude in the HEADING OS "
                 f"folder, and the setup wizard starts there. {HANDOFF_CLOSE} If the wizard does not start "
                 "in it, type heading /setup-wizard there.")
HANDOFF_IN_HERDR = (f"{DONE_SAY} You are already in Herdr, so no second Herdr starts: a new workspace named "
                    "HEADING OS is opening in it with Claude in the HEADING OS folder, and the setup wizard "
                    "starts there. The pane where the install ran is no longer needed and can be closed; "
                    "the setup continues in the new workspace.")
HANDOFF_PLAIN = (f"{DONE_SAY} Herdr could not be started, so a new terminal window is opening with Claude "
                 f"in the HEADING OS folder, without Herdr, and the setup wizard starts there. {HANDOFF_CLOSE}")
HANDOFF_TYPE = (f"{DONE_SAY} To continue with the setup, open a new terminal and type: heading /setup-wizard. "
                "The terminal where the install ran is then no longer needed and can be closed.")


def _announce(ctx: Ctx, say: str) -> None:
    """Tell the person what is about to open, before it opens: stdout and the progress window.

    Returns once the window has drawn it (at most progress.SHOWN_WAIT_S), so
    the handoff never opens under a window still showing the last step.
    """
    print(say, flush=True)
    progress.wait_shown(ctx, progress.write(ctx, "done", len(STEPS), len(STEPS), "", say))


def hand_off(ctx: Ctx) -> str:
    """Open Claude with /setup-wizard inside Herdr, announced first; return the `done` say.

    Three cases (HOS-336), each announced on stdout and in the progress window
    before anything opens:
      * the installer runs inside a Herdr pane (HERDR_PANE_ID or HERDR_ENV set):
        no second Herdr ever starts; a HEADING OS workspace is created in the
        running one and the wizard typed into it;
      * a Linux desktop that is not WSL, with x-terminal-emulator and the Herdr
        the install's herdr step put in place: a new window runs that Herdr by
        absolute path, and a detached helper (scripts/utils/heading_launcher.py
        --herdr-workspace) waits for it, creates the workspace in the engine
        folder and runs `heading /setup-wizard` in its pane;
      * the same desktop without a runnable Herdr: today's plain Claude window.
    Every other case, and a window that cannot be started, starts nothing and
    the say tells the person to type `heading /setup-wizard` in a new terminal.
    Windows Terminal from WSL is not attempted (design § 9: unverified).

    Every argv is a list, run with no shell. `-e <command> <args...>` is the
    form Debian Policy § 11.8.3 requires of x-terminal-emulator; Herdr in that
    window and the helper's socket calls are UNVERIFIED on Linux Mint until the
    machine rehearsal.
    """
    from scripts.utils import heading_launcher as launcher

    heading = Path.home() / ".local" / "bin" / "heading"
    herdr = hc.herdr_bin(hc.host(ctx))
    herdr_ok = os.access(herdr, os.X_OK)
    if launcher.in_herdr(os.environ) and herdr_ok:
        _announce(ctx, HANDOFF_IN_HERDR)
        try:
            launcher.open_in_herdr(herdr, ctx.engine, heading, env=ctx.env())
        except (launcher.HerdrHandoffError, OSError, subprocess.SubprocessError) as exc:
            print(f"  note: the HEADING OS workspace could not be opened in Herdr ({exc})")
            _announce(ctx, HANDOFF_TYPE)
            return HANDOFF_TYPE
        return HANDOFF_IN_HERDR
    if person.has_display() and not is_wsl() and which("x-terminal-emulator", ctx):
        if herdr_ok and not launcher.in_herdr(os.environ):
            _announce(ctx, HANDOFF_HERDR)
            try:
                person.start_detached(ctx, "handoff", ["x-terminal-emulator", "-e", str(herdr)],
                                      env=ctx.env(), cwd=ctx.engine)
            except OSError as exc:
                print(f"  note: the new window with Herdr could not be opened ({exc})")
            else:
                helper = [str(ctx.python), str(ctx.engine / "scripts" / "utils" / "heading_launcher.py"),
                          "--herdr-workspace", str(herdr), "--engine", str(ctx.engine), "--heading", str(heading)]
                try:
                    person.start_detached(ctx, "handoff-herdr", helper, env=ctx.env(), cwd=ctx.engine)
                except OSError as exc:
                    # The window with Herdr is open; the say already tells them to type the command there.
                    print(f"  note: the setup wizard could not be started in Herdr ({exc})")
                return HANDOFF_HERDR
        _announce(ctx, HANDOFF_PLAIN)
        try:
            person.start_detached(ctx, "handoff", ["x-terminal-emulator", "-e", str(heading), "/setup-wizard"],
                                  env=ctx.env(), cwd=ctx.engine)
        except OSError as exc:
            print(f"  note: the new window could not be opened ({exc})")
        else:
            return HANDOFF_PLAIN
    _announce(ctx, HANDOFF_TYPE)
    return HANDOFF_TYPE


# ---- HOS-312: the intake, the status line and the install report ----------

STATUS_PREFIX = "HEADING-INSTALL: "
STATE_EXIT = {"done": 0, "failed": 1, "need-answer": 3, "waiting-for-person": 4}
ANSWER = "{answer}"
FREE = "{free}"
REPORT_NAME = "install-report.txt"
#: Every flag a rerun carries, in this order: (flag, argparse dest).
RERUN_FLAGS = (("--target", "target"), ("--mail", "mail"), ("--github-backup", "github_backup"),
               ("--tz", "tz"), ("--proxy-signin", "proxy_signin"), ("--answers", "answers"),
               ("--repo-name", "repo_name"), ("--ref", "ref"), ("--from", "source"),
               ("--google-client", "google_client"), ("--google-audience", "google_audience"))

MAIL_QUESTION = ("Which mail system do you use? HEADING OS reads your mail and calendar, and nothing "
                 "is ever sent without your approval. You can also connect mail later.",
                 [{"label": "Connect later", "value": "none"},
                  {"label": "Exchange", "value": "exchange"},
                  {"label": "Google", "value": "google"}])
GITHUB_QUESTION = ("Do you have a personal GitHub account where you would like a private backup of "
                   "your data? Your data stays on this computer either way.",
                   [{"label": "Later", "value": "later"}, {"label": "Yes", "value": "yes"}])


def status_line(state: str, say: str, next_cmd: str = "", options: list[dict] | None = None,
                code: int | None = None) -> int:
    """Print the one `HEADING-INSTALL:` line, the last line of stdout, and return the exit code.

    One JSON object on one line: `json.dumps` escapes every line break inside
    `say`. `options` only for need-answer. `code` overrides the state's exit
    code (the refused --target is failed with exit 2).
    """
    payload: dict = {"state": state, "say": say, "next": next_cmd}
    if state == "need-answer":
        payload["options"] = options or []
    print(STATUS_PREFIX + json.dumps(payload), flush=True)
    return STATE_EXIT[state] if code is None else code


def rerun_command(ap: argparse.ArgumentParser, args: argparse.Namespace, ask: str | None = None) -> str:
    """This run's command: the same interpreter and script, every flag that differs from its default.

    `ask` names the flag whose value becomes the placeholder {answer}. Every
    other word is shell-quoted; {answer} stays a bare token, so the reader
    substitutes the shell-quoted answer (the rule in the module docstring) and
    any answer, a single quote included, stays one literal argument.
    """
    words = [shlex.quote(sys.executable), shlex.quote(str(Path(__file__).resolve()))]
    for flag, dest in RERUN_FLAGS:
        value = getattr(args, dest)
        if flag == ask:
            words += [flag, ANSWER]
        elif value is not None and value != ap.get_default(dest):
            words += [flag, shlex.quote(str(value))]
    if args.dry_run:
        words.append("--dry-run")
    return " ".join(words)


def fill_answers(ctx: Ctx, args: argparse.Namespace,
                 filed: dict[str, str] | None = None) -> tuple[str, str, list[dict]] | None:
    """Fill the four install answers into `ctx`; return the first intake answer missing, else None.

    Each comes from its flag, else from the --answers file (`filed`), else from
    the engine's .env (a rerun), never from the process environment. The
    missing one is returned as (flag, question, options), in the order mail,
    GitHub backup, time zone. A --tz that is not a valid zone counts as missing.
    The proxy sign-in is never asked: absent everywhere, it is "now".
    """
    filed = filed or {}
    ctx.mail = args.mail or filed.get("mail") or install_answers.mail(ctx.engine, environ=False)
    ctx.github_backup = (args.github_backup or filed.get("github_backup")
                         or install_answers.github_backup(ctx.engine, environ=False))
    if args.tz is not None:
        ctx.tz = args.tz if install_answers.valid_zone(args.tz) else None
    else:
        ctx.tz = filed.get("tz") or install_answers.timezone(ctx.engine, environ=False)
    ctx.proxy_signin = (args.proxy_signin or filed.get("proxy_signin")
                        or install_answers.proxy_signin(ctx.engine, environ=False) or "now")
    if ctx.mail is None:
        return ("--mail", *MAIL_QUESTION)
    if ctx.github_backup is None:
        return ("--github-backup", *GITHUB_QUESTION)
    if ctx.tz is None:
        zone = _mail.system_zone() or "UTC"
        options = [{"label": "Yes", "value": zone}, {"label": "Another", "value": FREE}]
        if args.tz is None:
            return ("--tz", f"Is your time zone {zone}?", options)
        return ("--tz", f"{args.tz} is not a time zone this computer knows. "
                        "Name a city such as Europe/Berlin or Asia/Tokyo.", options)
    return None


def write_report(ctx: Ctx, s: Step, say: str) -> Path:
    """<target>/install-report.txt: the failed step, the platform and the four answers by name.

    Never a secret and never any other .env value. Mode 600 from creation
    (mkstemp), replaced whole each time with os.replace.
    """
    lines = [f"time: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
             f"step: {s.name}",
             f"title: {s.title}",
             f"platform: {platform.platform()}",
             f"mail: {ctx.mail or 'unset'}",
             f"github_backup: {ctx.github_backup or 'unset'}",
             f"tz: {ctx.tz or 'unset'}",
             f"proxy_signin: {ctx.proxy_signin or 'unset'}",
             "say:"]
    lines += [f"  {line}" for line in say.splitlines()]
    ctx.target.mkdir(parents=True, exist_ok=True)
    report = ctx.target / REPORT_NAME
    fd, tmp = tempfile.mkstemp(prefix=REPORT_NAME + ".", suffix=".tmp", dir=ctx.target)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        os.replace(tmp, report)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return report


def step_failed(ctx: Ctx, s: Step, say: str, next_cmd: str) -> int:
    print(say)
    print("Fix that, then run the same command again; finished steps are skipped.")
    try:
        print(f"The details are in {write_report(ctx, s, say)}")
    except OSError as exc:
        print(f"  note: the install report could not be written ({exc})")
    return status_line("failed", say, next_cmd)


def run_step(ctx: Ctx, s: Step, label: str, state: dict) -> bool:
    """Check, run and recheck one step. False when it ran and its result is still not in place."""
    try:
        in_place = s.check(ctx)
    except (StepFailed, OSError) as exc:
        print(f"  note: checking '{s.name}' failed ({exc}); running it")
        in_place = False
    if in_place:
        print(f"{label}: already in place")
        if not ctx.dry_run and s.name not in state["steps"]:
            state["steps"][s.name] = {"done_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            save_state(ctx, state)
        return True
    if ctx.dry_run:
        print(f"{label}: would be done now")
        return True
    repairing = s.name in state["steps"]
    s.run(ctx)
    ctx.bin_dirs = compute_bin_dirs(ctx)
    if not s.check(ctx):
        return False
    state["steps"][s.name] = {"done_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    save_state(ctx, state)
    print(f"{label}: {'repaired' if repairing else 'done'}")
    return True


AGREEMENT_ENV_DROP = (install_answers.MAIL_ENV, install_answers.GITHUB_BACKUP_ENV, install_answers.TZ_ENV,
                      install_answers.PROXY_SIGNIN_ENV, "HEADING_OS_DATA")


#: The last line scripts/install-agreement.py prints when it finds nothing (its main()).
AGREEMENT_CLEAN = "install-agreement: CLEAN, 0 findings"


def dry_run_agreement(ctx: Ctx, next_cmd: str) -> int:
    """The dry run's last part: scripts/install-agreement.py of THIS engine, as a child (HOS-312).

    Its scratch folder is a fresh <target>/.heading-install/agreement-XXXX
    (mkdtemp, mode 700) that only this run made; it is removed afterwards, and
    so are <target>/.heading-install and <target> when this run created them
    and they are empty, so nothing is left changed and nothing older is touched.

    The child's last stdout line tells findings from a crash: exit 0 with
    AGREEMENT_CLEAN, or exit 1 with "install-agreement: N findings"; any other
    ending is a check that could not run.
    """
    own = Path(__file__).resolve().parent.parent
    python = own / ".venv" / "bin" / "python"
    if not python.exists():
        return status_line("done", "Dry run: nothing was changed. The agreement check did not run: "
                                   "this engine has no environment yet.")
    state_dir = ctx.target / ".heading-install"
    made = [d for d in (ctx.target, state_dir) if not d.exists()]
    env = {k: v for k, v in os.environ.items() if k not in AGREEMENT_ENV_DROP}
    scratch = None
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        scratch = Path(tempfile.mkdtemp(prefix="agreement-", dir=state_dir))  # mode 700, this run's own
        proc = subprocess.run([str(python), str(own / "scripts" / "install-agreement.py"), "--scratch", str(scratch)],
                              cwd=str(own), env=env, capture_output=True, text=True, errors="replace")
    except OSError as exc:
        proc = subprocess.CompletedProcess([], 127, stdout="", stderr=str(exc))
    finally:
        if scratch is not None:
            try:
                shutil.rmtree(scratch)
            except OSError as exc:
                print(f"  note: {scratch} could not be removed ({exc.strerror})")
        for d in reversed(made):
            try:
                d.rmdir()
            except OSError as exc:
                print(f"  note: {d} was left in place ({exc.strerror})")
    print(proc.stdout, end="")
    summary = (proc.stdout.strip().splitlines() or [""])[-1]
    counted = re.fullmatch(r"install-agreement: ([1-9][0-9]*) findings", summary)
    if proc.returncode == 0 and summary == AGREEMENT_CLEAN:
        return status_line("done", "Dry run: nothing was changed. The parts agree in all 7 answer combinations.")
    if proc.returncode == 1 and counted:
        findings = [line.strip() for line in proc.stdout.splitlines() if line.startswith(" ")]
        return status_line("failed", "\n".join(["Dry run: the parts disagree:", *findings]), next_cmd)
    last = (proc.stderr.strip().splitlines() or [f"exit {proc.returncode}, and no summary line"])[-1]
    return status_line("failed", f"Dry run: the agreement check could not run: {last}", next_cmd)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Install HEADING OS (resumable; safe to run again).")
    ap.add_argument("--target", default=str(Path.home() / "HeadingOS"),
                    help="where engine/ and data/ go (default: ~/HeadingOS)")
    backup = ap.add_mutually_exclusive_group()
    backup.add_argument("--github-backup", choices=install_answers.GITHUB_BACKUP_CHOICES, default=None,
                        help="yes: also keep a private copy of the data on your GitHub account; "
                             "later: keep it on this computer only")
    backup.add_argument("--github", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--repo-name", default=DEFAULT_REPO_NAME, help="GitHub repository name for the data")
    ap.add_argument("--ref", default=None, help="tag or branch to install (default: the latest release)")
    ap.add_argument("--from", dest="source", default=DEFAULT_FROM, help="git URL or path of the engine")
    ap.add_argument("--dry-run", action="store_true", help="say what would be done; change nothing")
    ap.add_argument("--mail", choices=install_answers.MAIL_CHOICES, default=None,
                    help="connect an Exchange mailbox and calendar, Gmail and Google Calendar, "
                         "or none (connect later)")
    ap.add_argument("--tz", default=None, metavar="ZONE", help="your time zone, such as Europe/Berlin")
    ap.add_argument("--proxy-signin", choices=install_answers.PROXY_SIGNIN_CHOICES, default=None,
                    help="the Claude sign-in for the model proxy: now (the default) or later")
    ap.add_argument("--answers", default=None, metavar="PATH",
                    help="read the intake answers from this JSON file (mode 600); never stop to ask")
    ap.add_argument("--google-client", default=None, metavar="PATH",
                    help="with --mail google: the client file downloaded from your own Google sign-in app")
    ap.add_argument("--google-audience", choices=_google.AUDIENCES, default=None,
                    help="with --mail google: the status on the Google console's Audience page")
    args = ap.parse_args(argv)
    if args.github:
        args.github_backup = "yes"  # the hidden alias; every rerun command writes the normalised flag

    if any(ch in args.target for ch in "\n\r\0"):
        refusal = "The target folder name contains a line break; choose a plain folder name."
        print(refusal)
        return status_line("failed", refusal, "", code=2)
    ctx = Ctx(target=Path(args.target).expanduser().absolute(), source=args.source, ref=args.ref,
              github_backup=args.github_backup, repo_name=args.repo_name, dry_run=args.dry_run,
              google_client=(str(Path(args.google_client).expanduser().resolve())
                             if args.google_client else None),
              google_audience=args.google_audience)
    filed = None
    if args.answers is not None:
        args.answers = str(Path(args.answers).expanduser().absolute())  # a rerun carries it from anywhere
        try:
            filed = install_answers.read_answers_file(Path(args.answers))
        except install_answers.AnswersFileError as exc:
            print(exc)
            return status_line("failed", str(exc), "", code=2)
    missing = fill_answers(ctx, args, filed)
    if missing:
        flag, say, options = missing
        if args.answers is not None:
            key = flag[2:].replace("-", "_")
            refusal = (f"The answers file gives no {key}, and an install run from a file never stops to ask."
                       if flag != "--tz" or args.tz is None else
                       f"{args.tz} is not a time zone this computer knows, and an install run from a file "
                       "never stops to ask.")
            print(refusal)
            return status_line("failed", refusal, "", code=2)
        progress.write(ctx, "need-answer", 0, len(STEPS), "")
        return status_line("need-answer", say, rerun_command(ap, args, ask=flag), options)
    inherited = os.environ.get("HEADING_OS_DATA")
    if inherited and Path(inherited).expanduser().absolute() != ctx.data:
        print(f"  note: HEADING_OS_DATA points at {inherited}; this install uses {ctx.data} and leaves that one alone")
    ctx.bin_dirs = compute_bin_dirs(ctx)

    print(f"HEADING OS install into {ctx.target}" + (" (dry run: nothing is changed)" if ctx.dry_run else ""))
    state = load_state(ctx)
    total = len(STEPS)
    progress.start(ctx)
    for i, s in enumerate(STEPS, 1):
        label = f"[{i}/{total}] {s.title}"
        progress.write(ctx, "running", i, total, s.title)
        try:
            in_place = run_step(ctx, s, label, state)
        except WaitingForPerson as wait:
            progress.write(ctx, "waiting-for-person", i, total, s.title)
            return status_line("waiting-for-person", str(wait), rerun_command(ap, args))
        except (StepFailed, OSError, subprocess.SubprocessError) as exc:
            progress.write(ctx, "failed", i, total, s.title)
            return step_failed(ctx, s, f"{label}: FAILED. {exc}", rerun_command(ap, args))
        except Exception as exc:  # noqa: BLE001 - any other error still ends with the report and the status line
            traceback.print_exc()
            progress.write(ctx, "failed", i, total, s.title)
            return step_failed(ctx, s, f"{label}: FAILED. Unexpected error: {type(exc).__name__}: {exc}",
                               rerun_command(ap, args))
        if not in_place:
            progress.write(ctx, "failed", i, total, s.title)
            return step_failed(ctx, s, f"{label}: FAILED. It ran, but its result is still not in place.",
                               rerun_command(ap, args))

    if ctx.dry_run:
        return dry_run_agreement(ctx, rerun_command(ap, args))
    print(f"\nHEADING OS is installed: engine in {ctx.engine}, your data in {ctx.data}.")
    print_web_interfaces(ctx)
    hint = refresh_hint(ctx)
    if hint:
        print(f"To use it in THIS terminal, run once: {hint}   (new terminals need nothing)")
    return status_line("done", hand_off(ctx))


if __name__ == "__main__":
    raise SystemExit(main())
