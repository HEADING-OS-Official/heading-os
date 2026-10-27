#!/usr/bin/env python3
"""Deliver a task brief from HELM into a YARD, marked as machine-to-machine.

Usage:

    python scripts/herdr-brief.py w37:p1 "$(cat brief.md)" --issue HOS-30
    python scripts/herdr-brief.py w37:p1 "$(cat brief.md)" --no-issue "<why>"
    python scripts/herdr-brief.py w37:p1 "$(cat brief.md)" --check

The only sanctioned way HELM sends a brief to a YARD. It prepends the release
gate's `_BRIEF_MARKER` line and delegates to `herdr agent prompt <PANE> <TEXT>`.
HELM ONLY: `require_main_clone` refuses elsewhere, except for `--help` and
`--check`, which validate the text without contacting herdr or the tracker.

Why each check exists, in brief (the measurements behind each are in the git
history of this file):

* The marker: a prompt injected by another session is recorded exactly like
  one the operator typed (MEASURED 2026-09-05), so the sender marks it and the
  release gate refuses any release a marked prompt would authorise. It does NOT
  make an unmarked prompt the operator's; a bare `herdr agent prompt` is the
  hole, and no check covers it.
* Delivery is verified, never trusted: herdr answers with the pane it was
  GIVEN, and on 2026-09-05 and 2026-09-06 a brief landed in another yard. So
  the pre-flight refuses a pane with no live `claude` process in its checkout,
  and after sending the brief's unique id is looked for in the addressed
  transcript, naming where it landed if not there.
* `--issue` / `--no-issue` is required: dispatching a brief IS the allocation
  (`.claude/rules/lific.md`), so each issue must already read `active`. This
  script checks and never sets the status, so nothing is attributed to the
  operator that he did not do.
* `Tests:` line (rule M050, Phase 5 of `.claude/rules/development-standards.md`):
  exactly one of `Tests: none (Fix-Note)` or `Tests: failure A|B, <file>`; with
  `none`, a text that still asks for a test is refused. No bypass flag.
* `Master instruction:` line: warned on stderr when missing, never refused
  (a convention since 2026-10-12, not a gate).

* Task dossier (HOS-256): with `--issue`, the delivered text carries a
  `Task: HOS-N` line under the marker, so the yard's `dossier-bind.py` binds
  the issue from the first turn; once delivery is verified, the brief is saved
  under the registry name `brief-archive` with the same header and one
  `[dossier]` record of event `brief` goes to each issue.

It does not live in `scripts/utils/herdr_agent.py`, whose fixed `/compact`
literal must never become a parameter.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils import checkpoint_paths as CP  # noqa: E402
from scripts.utils import dossier  # noqa: E402
from scripts.utils.checkpoint_paths import transcript_dir  # noqa: E402
from scripts.utils.data_path import data_path  # noqa: E402
from scripts.utils.clone_guard import require_main_clone  # noqa: E402
from scripts.utils.colors import CYAN, GRAY, GREEN, RED, RESET, YELLOW  # noqa: E402
from scripts.utils.proc_cwd import processes_in  # noqa: E402

HERDR_BIN = "herdr"

# `herdr agent prompt` is a single socket call. Ten seconds is the budget
# `scripts/utils/herdr_agent.py` measured for the same subcommand.
PROMPT_TIMEOUT = 10

# How long the delivered text may take to reach the receiving session's
# transcript. MEASURED 2026-09-06 on the misdelivered brief: enqueue at
# 21:30:09, the user record at 21:30:15, so six seconds from send to a record on
# disk. Twenty-five gives that four times over; a brief that has not landed by
# then has not landed.
DELIVERY_TIMEOUT = 25
DELIVERY_POLL = 1.0

ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / ".claude" / "hooks" / "_dispatch.py"

BRIEF_ID_PREFIX = "X-HEADING-BRIEF-ID: "


def brief_marker() -> str:
    """The marker literal, read from the wall that acts on it.

    Loaded from `.claude/hooks/_dispatch.py` rather than spelled again here. A
    security literal written twice is one edit away from being written once:
    this repository's dominant defect shape is a fix that landed in one of N
    copies. The hook is not an importable package (`.claude` is not a Python
    path component), so it is loaded by file path, exactly as the test suite
    loads it. Import cost measured 2026-09-06: 24 ms.
    """
    spec = importlib.util.spec_from_file_location("heading_release_gate", GATE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load the release gate from {GATE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._BRIEF_MARKER


def compose(marker: str, text: str, issues: list[str] | None = None) -> str:
    """The marker on its own first line, the `Task:` line (`Task: none` too), then the brief verbatim.

    Always the header: without it a `Task:` line in the brief's own body would
    be the one dossier-bind reads.
    """
    return f"{marker}\n\n{dossier.task_header(issues or [])}\n\n{text}"


def archive_brief(payload: str, issues: list[str], checkout: Path, pane: str) -> None:
    """Save the delivered brief under `brief-archive` and post its `[dossier]` record. Never raises.

    After delivery is verified, so the file records a brief that arrived. A
    failure here costs the archive copy, never the brief itself.
    """
    try:
        proc = subprocess.run(  # nosec B603 B607 - fixed argv, no shell
            ["git", "-C", str(checkout), "branch", "--show-current"],
            capture_output=True, text=True, timeout=10)
        branch = proc.stdout.strip() or "unknown"
        yard = checkout.name
        stamp = CP.local_now().strftime("%Y-%m-%d-%H%M%S")
        directory = data_path("brief-archive")
        directory.mkdir(parents=True, exist_ok=True)
        name = f"{stamp}_brief_{CP.safe_slug(yard, 48)}_{'-'.join(issues) or 'none'}.md"
        path = directory / name
        body = (f"# Brief: {yard}\n\n"
                f"Sent: {CP.utc_now().isoformat()}\n"
                f"Session: {CP.session_id()}\n"
                f"Pane: {pane}\n"
                f"Branch: {branch}\n"
                f"{dossier.task_header(issues)}\n\n"
                f"## The brief as delivered\n\n{payload}")
        CP.write_text_atomic(path, body)
        print(f"{GREEN}Brief saved: {path}{RESET}")
        record = dossier.post(issues, "brief", yard, CP.session_id(),
                              [f"Yard: {yard}", f"Branch: {branch}"], dossier.rel(path))
        if issues:
            print(f"{GRAY}dossier: posted {len(record['posted'])}, "
                  f"queued {len(record['queued'])}{RESET}")
    except Exception as exc:  # noqa: BLE001 - the brief is delivered; the copy is secondary
        print(f"{YELLOW}herdr-brief: the brief was delivered but not archived "
              f"({type(exc).__name__}: {exc}){RESET}", file=sys.stderr)


def check_marker(marker: str, text: str) -> int:
    """Refuse a text that already carries the marker. 0 when it does not."""
    already = text.count(marker)
    if not already:
        return 0
    print(
        f"{RED}herdr-brief: REFUSED. The text already carries the marker "
        f"{already} time(s), so the brief this would send would carry it "
        f"{already + 1}.{RESET}\n"
        f"  A brief is marked once, here, at the moment it is sent. A text "
        f"that arrives already carrying the marker has been through this "
        f"path before or was hand-assembled, and either way what would go "
        f"out is not what its author wrote.\n"
        f"  Remove the marker line from the text and run this again.",
        file=sys.stderr)
    return 2


# ============================================================
# The Tests: line (M050, development-standards.md Phase 5)
# ============================================================

TESTS_LINE = re.compile(r"^[ \t]*Tests:(.*)$", re.MULTILINE)
TESTS_NONE = re.compile(r"none \(Fix-Note\)")
TESTS_FAILURE = re.compile(r"failure (A|B), (\S.*)")

#: What a brief saying `Tests: none` must not ask for. The one place the list
#: lives; keep it small. Word boundaries, because a bare "red on" is inside
#: "centered on" and "stored on". Bare "harness" is deliberately absent: briefs
#: say "the harness" for Claude Code itself, so only the test-shaped compounds
#: are listed.
TEST_REQUEST_PHRASES = (
    re.compile(r"\btests?[- ]first\b", re.IGNORECASE),
    re.compile(r"\b(?:write|add) (?:a |the )?(?:regression )?tests?\b",
               re.IGNORECASE),
    re.compile(r"\bin tests/", re.IGNORECASE),
    re.compile(r"\bred on\b", re.IGNORECASE),
    re.compile(r"\b(?:test|scratch|mutation) harness\b", re.IGNORECASE),
)

TESTS_HELP = (
    "  Every brief carries exactly one line, at a line start, of one of:\n"
    "      Tests: none (Fix-Note)\n"
    "      Tests: failure A, <file that holds the guarantee>\n"
    "      Tests: failure B, <file that holds the guarantee>\n"
    "  A test is written only for failure A (a private byte reaches the "
    "public engine) or failure B (the repositories merge, or a yard acts on "
    "something live); every other fix ships with a `Fix-Note:` line. Rule "
    "M050, and Phase 5 of `.claude/rules/development-standards.md`.")


def check_tests_line(text: str) -> int:
    """Refuse a brief whose `Tests:` line is missing, malformed, or contradicted.

    0 when the brief states one of the two accepted forms and, for `none`,
    asks for no test anywhere in its text. 7 otherwise, nothing sent.
    """
    lines = [m.group(1).strip() for m in TESTS_LINE.finditer(text)]
    if len(lines) != 1:
        found = "no `Tests:` line" if not lines else \
            f"{len(lines)} `Tests:` lines, so it says two things"
        print(f"{RED}herdr-brief: REFUSED. The brief has {found}. Nothing "
              f"was sent.{RESET}\n{TESTS_HELP}", file=sys.stderr)
        return 7
    value = lines[0]
    if TESTS_FAILURE.fullmatch(value):
        print(f"{GREEN}Tests: {value}{RESET}")
        return 0
    if not TESTS_NONE.fullmatch(value):
        print(f"{RED}herdr-brief: REFUSED. `Tests: {value}` is not one of the "
              f"two accepted forms. Nothing was sent.{RESET}\n{TESTS_HELP}",
              file=sys.stderr)
        return 7
    asks = []
    for phrase in TEST_REQUEST_PHRASES:
        for match in phrase.finditer(text):
            start = text.rfind("\n", 0, match.start()) + 1
            end = text.find("\n", match.end())
            asks.append((match.group(0),
                         text[start:end if end != -1 else None].strip()))
    if not asks:
        print(f"{GREEN}Tests: {value}{RESET}")
        return 0
    print(f"{RED}herdr-brief: REFUSED. The brief says `Tests: none (Fix-Note)` "
          f"and still asks for a test. Nothing was sent.{RESET}",
          file=sys.stderr)
    for phrase, line in asks:
        print(f"  \"{phrase}\" in: {line}", file=sys.stderr)
    print(f"  Reword the brief so it asks for no test, or, when the defect is "
          f"failure A or B, say so on the `Tests:` line. There is no bypass "
          f"flag.\n{TESTS_HELP}", file=sys.stderr)
    return 7


MASTER_LINE = re.compile(r"^[ \t]*Master instruction:[ \t]*\S", re.MULTILINE)


def warn_master_line(text: str) -> None:
    """Warn, never refuse, when the brief names no master instruction."""
    if MASTER_LINE.search(text):
        return
    print(f"{YELLOW}herdr-brief: warning. The brief has no `Master "
          f"instruction:` line. Name the archived prompt it executes "
          f"(`python scripts/prompt-archive.py latest`), or write "
          f"`Master instruction: none, <why>`. The brief is not refused."
          f"{RESET}",
          file=sys.stderr)


def target_dir(checkout: Path, projects_root: Path | None) -> Path | None:
    """Where Claude Code keeps that checkout's transcripts.

    The slug rule is owned by `checkpoint_paths.transcript_dir` and is not
    copied here: a second copy of it was caught in this file on 2026-09-06, and
    no check looks for one now. `projects_root` relocates only the ROOT, for a
    fixture tree. None when the owner declines (off POSIX).
    """
    owned = transcript_dir(checkout)
    if owned is None:
        return None
    return (projects_root / owned.name) if projects_root else owned


def checkout_for_pane(pane: str, workspaces: list) -> Path | None:
    """The checkout path of the workspace owning `pane`, from herdr's own list.

    Reading the list is not the same as trusting the SEND. The list is the only
    place the pane-to-checkout mapping exists; what it cannot tell us, and what
    this script therefore reads from the transcripts instead, is where a prompt
    actually went.
    """
    workspace_id = pane.split(":", 1)[0]
    for entry in workspaces:
        if entry.get("workspace_id") != workspace_id:
            continue
        worktree = entry.get("worktree") or {}
        path = worktree.get("checkout_path")
        if path:
            return Path(path)
        return agent_cwd(pane)
    return None


def agent_cwd(pane: str) -> Path | None:
    """The working directory of the agent in `pane`, asked of herdr.

    The fallback for a workspace with no `worktree` block. MEASURED 2026-09-06:
    a yard whose herdr workspace had died was re-attached with
    `herdr workspace create --cwd <checkout>`, which produces a workspace herdr
    reports WITHOUT a worktree block. `checkout_path` was then None and this
    script refused, so a yard with real uncommitted work in it could not be
    briefed at all -- while `git worktree remove` refused to delete it, which is
    correct, leaving the checkout reachable by nothing.

    The agent's own cwd is not a weaker answer than the worktree block; it is a
    better one. `transcript_dir()` derives the slug from a PATH, and the path
    that decides where this agent writes is the path it is running in.

    Trusting herdr for the cwd does not reopen what this script exists to close.
    A wrong cwd here sends the delivery check looking in the wrong transcript
    directory, the brief id is not found, and the run exits 4 naming where it
    did land. The failure mode is a loud false alarm, never a silent success.
    """
    try:
        result = subprocess.run([HERDR_BIN, "agent", "get", pane],
                                capture_output=True, text=True,
                                timeout=PROMPT_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        agent = (json.loads(result.stdout).get("result") or {}).get("agent")
    except ValueError:
        return None
    cwd = (agent or {}).get("cwd")
    return Path(cwd) if cwd else None


def herdr_workspaces() -> list:
    """`herdr workspace list`, parsed. Empty list on any failure."""
    try:
        result = subprocess.run([HERDR_BIN, "workspace", "list"],
                                capture_output=True, text=True,
                                timeout=PROMPT_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    try:
        return (json.loads(result.stdout).get("result") or {}).get(
            "workspaces") or []
    except (ValueError, AttributeError):
        return []


def transcripts(project_dir: Path) -> list[Path]:
    """The session transcripts under a project directory, newest last."""
    try:
        return sorted(project_dir.glob("*.jsonl"))
    except OSError:
        return []


def carries(paths: list[Path], token: str) -> bool:
    """Whether any of those transcripts contains `token`."""
    needle = token.encode("utf-8")
    for path in paths:
        try:
            with path.open("rb") as handle:
                while chunk := handle.read(1 << 20):
                    if needle in chunk:
                        return True
        except OSError:
            continue
    return False


def landed_elsewhere(projects_root: Path, token: str,
                     target: Path) -> list[str]:
    """Every project directory OTHER than the target whose transcript has it.

    This is the half that turned a silent misdelivery into a named one: on
    2026-09-06 the brief was found in another yard, one with the
    operator's live work in it, while herdr had reported success for another.
    """
    found = []
    try:
        candidates = sorted(p for p in projects_root.iterdir() if p.is_dir())
    except OSError:
        return found
    for directory in candidates:
        if directory == target:
            continue
        if carries(transcripts(directory), token):
            found.append(directory.name)
    return found


def verify_delivery(target: Path, token: str, projects_root: Path,
                    timeout: float = DELIVERY_TIMEOUT) -> int:
    """Poll the ADDRESSED session's own transcript for the brief's id.

    Returns 0 when the text arrived there. Non-zero otherwise, having said where
    it went if it can find out.
    """
    deadline = time.monotonic() + timeout
    while True:
        if carries(transcripts(target), token):
            print(f"{GREEN}Delivery verified in {target.name}.{RESET}")
            return 0
        if time.monotonic() >= deadline:
            break
        time.sleep(DELIVERY_POLL)

    strays = landed_elsewhere(projects_root, token, target)
    print(f"{RED}herdr-brief: DELIVERY NOT VERIFIED after {timeout:.0f}s.{RESET}",
          file=sys.stderr)
    print(f"  The brief does not appear in the transcript of the workspace it "
          f"was addressed to ({target.name}).", file=sys.stderr)
    if strays:
        print(f"{RED}  IT LANDED IN: {', '.join(strays)}{RESET}",
              file=sys.stderr)
        print("  That session is now holding a brief written for another tree. "
              "Say so to whoever owns it; do not send again until you know why.",
              file=sys.stderr)
    else:
        print("  It was not found in any other project directory either, so it "
              "may still be in flight, or it may never have been delivered.",
              file=sys.stderr)
    print("  herdr's own reply names the pane it was GIVEN, not the pane that "
          "received the text, so a success line above establishes nothing.",
          file=sys.stderr)
    return 4


LIFIC_BIN = "lific"

#: The one status that means "a yard has this". `.claude/rules/lific.md` § the
#: five statuses. Spelled once, here, and read by both the check and its message.
ALLOCATED = "active"


def issue_status(identifier: str) -> tuple[str | None, str]:
    """The current status of one Lific issue, and why if it cannot be read.

    A READ, and that is deliberate: see the module docstring. Returns
    `(None, reason)` when the tracker cannot answer, and the caller REFUSES on
    that rather than sending. Failing toward refusal is the right direction for
    a check whose whole job is to stop a brief going out unrecorded; a tracker
    that is down is not evidence that the issue is allocated.
    """
    if shutil.which(LIFIC_BIN) is None:
        return None, f"`{LIFIC_BIN}` is not on PATH"
    try:
        result = subprocess.run(  # nosec B603 - fixed argv, no shell
            [LIFIC_BIN, "issue", "get", identifier, "--json"],
            capture_output=True, text=True, timeout=PROMPT_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"`{LIFIC_BIN} issue get` did not answer: {exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        return None, (f"`{LIFIC_BIN} issue get {identifier}` exited "
                      f"{result.returncode}: {detail[0] if detail else 'no output'}")
    try:
        status = json.loads(result.stdout).get("status")
    except ValueError as exc:
        return None, f"the tracker's answer for {identifier} is not JSON: {exc}"
    if not isinstance(status, str):
        return None, f"the tracker's answer for {identifier} carries no status"
    return status, ""


def check_allocated(identifiers: list[str]) -> int:
    """Refuse unless every named issue is already allocated to a yard.

    Prints one line per issue either way, because a check that is silent when it
    passes is a check nobody can tell ran.
    """
    blocked: list[str] = []
    for identifier in identifiers:
        status, reason = issue_status(identifier)
        if status is None:
            print(f"{RED}  {identifier}: cannot be read ({reason}){RESET}",
                  file=sys.stderr)
            blocked.append(identifier)
            continue
        if status != ALLOCATED:
            print(f"{RED}  {identifier}: {status}{RESET}", file=sys.stderr)
            blocked.append(identifier)
            continue
        print(f"{GREEN}  {identifier}: {status}{RESET}")

    if not blocked:
        return 0

    print(
        f"{RED}herdr-brief: REFUSED. Nothing was sent.{RESET}\n"
        f"  Dispatching a brief IS the allocation, so the issue it allocates "
        f"has to read `{ALLOCATED}` before the brief leaves HELM. Otherwise the "
        f"board says a task is waiting to be triaged while a yard is already "
        f"working on it, which happened twice on 2026-09-20 and both times the "
        f"operator found it rather than the machine.\n"
        f"  Move each of these, then send again. From a HELM session, through "
        f"MCP and not the CLI, so the change is attributed to the agent and not "
        f"to the operator:\n"
        f"      mcp__lific__update_issue(identifier=\"{blocked[0]}\", "
        f"status=\"{ALLOCATED}\")\n"
        f"  If this brief genuinely allocates nothing, say so instead: "
        f"`--no-issue \"<why>\"`.",
        file=sys.stderr)
    return 6


def send(pane: str, payload: str) -> int:
    """Hand the composed brief to HERDR. List argv, never `shell=True`."""
    if shutil.which(HERDR_BIN) is None:
        print(f"{RED}herdr-brief: `{HERDR_BIN}` is not on PATH.{RESET}",
              file=sys.stderr)
        return 3
    argv = [HERDR_BIN, "agent", "prompt", pane, payload]
    try:
        result = subprocess.run(argv, capture_output=True, text=True,
                                timeout=PROMPT_TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        print(f"{RED}herdr-brief: `{HERDR_BIN} agent prompt` did not answer "
              f"within {PROMPT_TIMEOUT}s. Nothing is confirmed sent.{RESET}",
              file=sys.stderr)
        return 3
    if result.stdout:
        print(result.stdout.rstrip())
    if result.returncode != 0:
        print(f"{RED}herdr-brief: herdr exited {result.returncode}.{RESET}",
              file=sys.stderr)
        if result.stderr:
            print(result.stderr.rstrip(), file=sys.stderr)
        return 3
    print(f"{GREEN}Sent to {pane}.{RESET}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="herdr-brief.py",
        description="Send a marked machine-to-machine brief to a YARD pane.",
        epilog=("The marker makes the brief unable to authorise a commit or a "
                "push in the receiving session. It does NOT make an UNMARKED "
                "prompt trustworthy: no field in a transcript record tells the "
                "operator's typing apart from another session's."),
    )
    parser.add_argument("pane", help="HERDR pane id, e.g. w37:p1")
    parser.add_argument("text", help="the brief, verbatim")
    parser.add_argument(
        "--issue", action="append", default=[], metavar="HOS-N",
        help=("the Lific issue this brief allocates to the yard. Repeatable. "
              "Each must already read `active`; this script checks and never "
              "sets, so the status stays attributed to whoever moved it."))
    parser.add_argument(
        "--no-issue", default=None, metavar="REASON",
        help=("send a brief that allocates no issue, stating why. The reason is "
              "printed with the brief. Mutually exclusive with --issue."))
    parser.add_argument(
        "--projects-root", default=None,
        help=("where Claude Code writes session transcripts (default "
              "~/.claude/projects). Present so the delivery checks can be "
              "driven over a fixture tree; there is no flag to SKIP them."))
    parser.add_argument(
        "--check", action="store_true",
        help=("validate the brief text (the marker, the `Tests:` line) and "
              "exit without sending or asking the tracker. Runs from any "
              "checkout."))
    args = parser.parse_args()

    marker = brief_marker()

    # The text checks come first and need nothing live, so `--check` stops
    # after them, BEFORE the HELM guard: it is a read, like `--help`.
    status = check_marker(marker, args.text) or check_tests_line(args.text)
    if status != 0:
        return status
    warn_master_line(args.text)
    if args.check:
        print(f"{GREEN}Brief text accepted. Nothing was sent (--check).{RESET}")
        return 0

    # After `parse_args`, so `--help` answers from anywhere. A help text is a
    # read; the guard exists to stop a SEND, and a script whose `--help` refuses
    # in a YARD is a script nobody can read the usage of while working on it.
    require_main_clone(__file__)

    # The allocation, AFTER the text checks and before anything is sent. The
    # order is deliberate: a malformed brief should be refused without the
    # tracker being asked anything at all.
    if args.issue and args.no_issue is not None:
        print(f"{RED}herdr-brief: REFUSED. --issue and --no-issue are the two "
              f"answers to one question, and this run gave both.{RESET}",
              file=sys.stderr)
        return 6
    if not args.issue and args.no_issue is None:
        print(
            f"{RED}herdr-brief: REFUSED. Say which issue this brief allocates."
            f"{RESET}\n"
            f"  Dispatching a brief is what moves an issue to `{ALLOCATED}`, so "
            f"this script will not send one without knowing which. There is no "
            f"default: defaulting to none is the miss this closes, and guessing "
            f"from the text would allocate whatever the brief happened to "
            f"mention.\n"
            f"      --issue HOS-30            (repeatable)\n"
            f"      --no-issue \"<why this allocates nothing>\"",
            file=sys.stderr)
        return 6
    if args.issue:
        print(f"{CYAN}Allocation:{RESET}")
        allocation = check_allocated(args.issue)
        if allocation != 0:
            return allocation
    else:
        print(f"{YELLOW}Allocation: none, stated as "
              f"\"{args.no_issue}\".{RESET}")

    # None, not a default: the owner resolves the whole path, and only a
    # fixture relocates the root.
    projects_root = Path(args.projects_root) if args.projects_root else None

    checkout = checkout_for_pane(args.pane, herdr_workspaces())
    if checkout is None:
        print(f"{RED}herdr-brief: REFUSED. herdr does not report a workspace "
              f"owning {args.pane}, so there is no checkout to verify delivery "
              f"against.{RESET}", file=sys.stderr)
        return 5
    target = target_dir(checkout, projects_root)
    if target is None:
        print(f"{RED}herdr-brief: REFUSED. The transcript directory for "
              f"{checkout} cannot be resolved on this platform, so delivery "
              f"cannot be verified and herdr's reply is the only evidence "
              f"there would be. That is what this script exists not to "
              f"trust.{RESET}", file=sys.stderr)
        return 5

    # The pre-flight: is an agent THERE NOW (both misdeliveries, 2026-09-05 and
    # 2026-09-06, went to a pane with none). Asked of /proc: a `claude` process
    # whose cwd IS the checkout. A transcript is the wrong proxy both ways (a
    # fresh agent has none, an exited one leaves one), so it is consulted only
    # when /proc cannot be read, which `processes_in` reports as None.
    agents = processes_in(checkout, comm="claude", nested=False)
    if agents is None:
        if not transcripts(target):
            print(
                f"{RED}herdr-brief: REFUSED. Cannot establish an agent in "
                f"{checkout}.{RESET}\n"
                f"  /proc could not be enumerated here, so the direct check is "
                f"unavailable and the weaker proxy was used: its Claude project "
                f"directory ({target}) holds no transcript. Sending anyway is "
                f"what misdelivers: MEASURED 2026-09-06, a brief addressed to a "
                f"pane with no agent was answered with a success line naming "
                f"that pane and arrived in a DIFFERENT yard that had live work "
                f"in it.\n"
                f"  Start an agent in that pane first, then send.",
                file=sys.stderr)
            return 5
    elif not agents:
        print(
            f"{RED}herdr-brief: REFUSED. No Claude agent is running in "
            f"{checkout}.{RESET}\n"
            f"  No process named `claude` has that directory as its working "
            f"directory, so the pane has nothing to receive this. A transcript "
            f"in {target} would not change that: an agent that has EXITED "
            f"leaves one behind, and that is the shape of both recorded "
            f"misdeliveries.\n"
            # No `exec` in the hint. It replaces the pane's shell, so the next
            # ordinary agent exit takes the whole workspace with it; measured
            # three times on 2026-09-06 (see yard-bootstrap.sh step 11).
            f"  Start an agent in that pane first, then send: "
            f"`herdr pane run <ws>:p1 "
            f"\"HEADING_OS_YARD=1 claude --dangerously-skip-permissions\"`.",
            file=sys.stderr)
        return 5

    brief_id = f"{BRIEF_ID_PREFIX}{uuid.uuid4()}"
    payload = f"{compose(marker, args.text, args.issue)}\n\n{brief_id}\n"
    print(f"{CYAN}About to send to {args.pane} ({checkout.name}):{RESET}")
    print(f"{GRAY}{'-' * 72}{RESET}")
    print(payload)
    print(f"{GRAY}{'-' * 72}{RESET}")

    status = send(args.pane, payload)
    if status != 0:
        return status

    print(f"{YELLOW}Verifying delivery in {target.name} ...{RESET}")
    delivered = verify_delivery(target, brief_id, projects_root or target.parent)
    if delivered == 0:
        archive_brief(payload, args.issue or [], checkout, args.pane)
    return delivered


if __name__ == "__main__":
    sys.exit(main())
