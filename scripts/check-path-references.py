#!/usr/bin/env python3
"""Advisory audit: engine prose naming repo paths that do not exist.

Documentation rot is silent. A rule, skill or doc page names `scripts/foo.py`,
the script is later renamed or deleted, and the prose keeps pointing at nothing.
Nobody notices until someone pastes the command and it fails. On 2026-08-21 a
sweep found eight such sites that had accumulated over months, including a
`/odin` source-ingest command that could not run and a `docs/SECURITY-MODEL.md`
paragraph describing two hook files deleted in ba1affd.

Scope, precisely (`.claude/rules/scope-claims.md`): this checks paths named in
**git-tracked Markdown** against the **engine working tree**, excluding only a
path the routing contract resolves to the private data repository and an engine
path an explicit rule keeps out of the engine, because the data repository is
absent on a public clone and its absence is not evidence (`in_engine_scope`).
It says nothing about paths named in Python, YAML or JSON; those have their own
callers.

A path the contract refuses (no rule, not allowlisted) is CHECKED rather than
skipped, the over-reporting direction scope-claims requires (2026-09-02: when
the map's `default:` decided scope, one word there switched the gate off
silently). `main()`
also refuses rather than passing when the corpus collapses to zero files or
zero in-scope references, or when the map is missing or unparseable.

Extraction is a regex over prose, so it is heuristic in BOTH directions: it can
MISS a path (an unusual spelling), and it can capture a fragment that was never
a path (`action_queue.append(` truncates to `action_queue.appen`). Both classes
live in BASELINE with a stated reason rather than being silently filtered, so
the list of what this tool ignores is readable.

One class is filtered instead of listed: a path `.gitignore` covers. Runtime
state such as `.claude/scheduled_tasks.json` is present on the operator's
machine and absent from every clone, so BASELINE cannot express it -- the entry
reads stale locally and the path reads dangling in CI. Prose naming a gitignored
path is correct by construction, so `scan()` drops it.

BASELINE is a frozen ratchet, the same shape as `audit-skill-bash-paths.py`: a
dangling path already listed is tolerated, a NEW one fails `--check`. Removing
an entry (cleaning the prose) is welcome -- delete the line in the same change.

Usage:
  python scripts/check-path-references.py           # list every dangling reference
  python scripts/check-path-references.py --check   # exit 1 on any NEW one
  python scripts/check-path-references.py --json    # machine-readable

No test covers this gate.
"""
import argparse
import functools
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.utils.workspace import (  # noqa: E402
    RoutingRefused, get_workspace_root, get_routing_destination,
    load_routing_map, matched_routing_rule,
)
from scripts.utils.data_homes import homes  # noqa: E402
from scripts.utils.data_path import DataPathError  # noqa: E402
from scripts.utils.paths import get_data_root, data_root_is_demo  # noqa: E402
from scripts.utils.colors import GREEN, YELLOW, RED, CYAN, GRAY, BOLD, RESET  # noqa: E402
from scripts.utils.repo_files import ignored_paths_or_none  # noqa: E402

# Repo-relative paths, anchored on the top-level directories the engine owns.
# The trailing class refuses a bare trailing dot so `foo.py.` yields `foo.py`.
#
# The extension length was {2,5} until 2026-08-23, which SILENTLY TRUNCATED any
# longer one: prose naming `reference/sentinel.service` was scanned as
# `reference/sentinel.servi`, a path that of course does not exist, so a real
# file was reported as rot. Under-matching here is the worse direction, because
# it manufactures a finding against correct prose. Widened to 9, which covers
# `.service` and `.template`.
_PATH = re.compile(
    r"(?<![\w/.-])"
    r"((?:scripts|config|docs|reference|tests|examples|templates|\.claude|\.github)"
    r"/[A-Za-z0-9_./-]*[A-Za-z0-9_-]\.[a-z]{2,9})"
)

# History by design: a changelog names what a release removed. Never rot.
_SKIP_FILES = {"CHANGELOG.md"}

# Frozen 2026-08-21, after the eight real rot sites were fixed. Every entry is a
# path that does not exist and SHOULD NOT: a placeholder, a regex fragment, or
# correct prose about something deleted or not yet built. Reason is mandatory --
# an entry without one is indistinguishable from rot somebody gave up on.
BASELINE = {
    # Regex fragments: a sentence continued past what looked like a path.
    # These two were spelled `.appen` / `.assig` until 2026-08-23, when the
    # extension cap above went from 5 to 9 and the scanner started seeing the
    # whole word.
    "scripts/record_readers/sources/action_queue.append": "fragment of `.append(`",
    "scripts/scrutinize-dispatch.assign": "fragment of `.assign`",
    # `scripts/scrutinize-dispatch.swap` sat here until 2026-09-11, covering the
    # prose fragment `scrutinize-dispatch.swap_for_run()`. That function was
    # replaced by `assignment_for_run` when the judge roster gained its third
    # family, every mention of it in the prose corpus went with it, and this
    # guard reports a baseline entry matching nothing so the registry cannot
    # accumulate exemptions guarding nothing.
    "scripts/utils/tool_risk.tier": "fragment of `tool_risk.tier_for()`",
    # Five more left on 2026-09-25 because the scan stopped finding them once the
    # non-engine files left the engine (HOS-131): `.claude/settings.local`,
    # `scripts/install-...-timer.sh`, `reference/playbook-gcc.md`,
    # `scripts/export-sync.py` and `config/classification.json`.
    # Deliberate placeholders in templates and naming-convention examples.
    "scripts/models/product.py": "example file in the plan template",
    "scripts/models/user.py": "example file in the plan template",
    "scripts/name.py": "stands for `scripts/<name>.py` in the naming rule",
    "scripts/dashboard.py": "hypothetical finding in the scrutinize eval template",
    "scripts/linkedin_archive.py": "quoted error message demonstrating a snake_case failure",
    # Correct prose about things deleted, retired, or not yet built.
    ".claude/rules/vpn-preflight.md": "named as the path this file moved from",
    "scripts/telegram_client.py": "named as the WRONG path, to warn against it",
    # Added by the official build: files the official manifest leaves out.
    ".claude/rules/corporate-docs.md": "the official distribution leaves this engine file out",
    ".claude/skills/ast-grep/SKILL.md": "the official distribution leaves this engine file out",
    "docs/DEPLOYMENT.md": "the official distribution leaves this engine file out",
    "scripts/utils/modem_ssh.py": "the official distribution leaves this engine file out",
}


ROUTING_MAP_REL = "config/routing-map.yaml"


class ScopeCollapsed(RuntimeError):
    """The scanner could not establish a corpus, so it must not report a pass.

    Raised instead of returning an empty result. Every caller of `scan_scoped`
    either handles this or dies on it; neither outcome can be mistaken for a
    clean tree, which is the whole point. The message names what collapsed and
    which file to look at.
    """


def require_routing_map(root: Path) -> dict:
    """The routing map's rules, or `ScopeCollapsed` naming the file.

    `load_routing_map()` cannot be asked this question. It fails CLOSED and
    SILENTLY: an absent, unreadable or unparseable map returns
    `{"default": "private", "rules": {}}`, indistinguishable from a real map
    with no rules. That is the right answer for the leak wall, which must treat
    an unknown path as data. It is the wrong answer here, because this scanner
    would then read "no rule excludes anything", check every path in the tree,
    and report every overlay path as rot. Refusing is the honest third option.

    Reading the file a second time rather than teaching `load_routing_map()` to
    raise is deliberate: 23 callers depend on that loader failing closed without
    an exception, and several of them are the push-time leak gate.

    The root is the one `get_workspace_root()` returns, because that is the map
    `matched_routing_rule()` and `get_routing_destination()` consult. Passing a
    different tree to `scan_scoped()` does not move the map they read, so this
    check must not pretend otherwise.
    """
    import yaml

    from scripts.utils import yamlio

    path = root / ROUTING_MAP_REL
    try:
        with open(path, encoding="utf-8") as fh:
            data = yamlio.safe_load(fh)
    except OSError as exc:
        raise ScopeCollapsed(
            f"the routing map {path} could not be read ({exc}); this scanner "
            f"decides what to inspect from that file's rules and will not "
            f"report a pass without it") from exc
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ScopeCollapsed(
            f"the routing map {path} did not parse ({type(exc).__name__}); "
            f"this scanner decides what to inspect from that file's rules and "
            f"will not report a pass without it") from exc
    rules = data.get("rules") if isinstance(data, dict) else None
    if not isinstance(rules, dict) or not rules:
        raise ScopeCollapsed(
            f"the routing map {path} carries no `rules:` mapping, so nothing "
            f"marks a path as overlay-owned and this scanner cannot state the "
            f"scope it claims")
    return rules


@functools.lru_cache(maxsize=None)
def in_engine_scope(path: str) -> bool:
    """True when this scanner may judge `path`'s absence as documentation rot.

    Only an EXPLICIT rule excludes a path. A path no rule mentions is judged,
    not skipped: absence of a rule is absence of evidence, and
    `.claude/rules/scope-claims.md` says to fail toward over-reporting there.

    The predecessor asked `get_routing_destination(path) != "engine"`, which
    routes an unmatched path through the map's top-level `default:`. Flipping
    that one word to `private` therefore excluded every path in the tree and the
    scanner printed OK over an empty scope. See the module docstring for the
    measurement.

    Cached because the live map holds ~90 rule keys and each call walks all of
    them twice, once per helper, over the ~10^4 references the corpus extracts.
    The map cannot change under a single run of this script, so a per-path cache
    is sound here; it would not be inside a long-running daemon.
    """
    # Since HOS-232 the data repository is private by REPOSITORY and the map
    # holds no data rules, so the two exclusions are a data path and an
    # engine path an explicit rule keeps private. A path the contract refuses
    # because it lies in neither tree is still judged: a dangling engine
    # reference is exactly what this scanner reports. Only a path that is not
    # an engine-relative path at all (`..` out of the tree, absolute
    # elsewhere) is left alone.
    try:
        rule = matched_routing_rule(path)
        if rule is not None and load_routing_map()["rules"][rule] != "engine":
            return False
        return get_routing_destination(path) != "private"
    except RoutingRefused as exc:
        return exc.reason not in ("escape", "outside")


def gitignored(root: Path, paths: list[str]) -> set[str]:
    """Of `paths`, those `.gitignore` covers -- absent from a clone by design.

    Outside a git repo (a synthetic root in a test) git cannot answer, so
    nothing is filtered: the tool over-reports rather than going quiet, per
    `.claude/rules/scope-claims.md`. That choice is made HERE, in one visible
    line, which is why this calls the `_or_none` form.

    The `git check-ignore` invocation itself used to be spelled out here, a
    second copy of the one in `scripts/utils/repo_files.py`. The two had drifted
    to opposite contracts -- this one degraded silently, the other raised -- and
    a bug fixed in one would not have reached the other. The contracts still
    differ, deliberately; the CALL no longer does.
    """
    return ignored_paths_or_none(paths, root) or set()


def tracked_markdown(root: Path) -> list[str]:
    """Tracked `*.md`, or an empty list when git cannot answer.

    "Cannot answer" covers BOTH a git that ran and refused and a git that could
    not be run at all. The second half arrived on 2026-09-02: the docstring
    below already promised graceful degradation, and the code under it read the
    refusal off a return code, which no missing executable ever produces.
    `subprocess.run` raises before the process starts, so with no git on PATH
    this function died on a traceback in a tool whose every other failure is a
    printed line and a stated exit code. MEASURED 2026-09-02 with
    `PATH=/nonexistent`: `FileNotFoundError: [Errno 2] No such file or
    directory: 'git'`. `OSError` rather than `FileNotFoundError` alone, because
    a git that is present and not executable raises `PermissionError` and says
    just as little.

    Returning [] hands `main()` a corpus of zero, which it already refuses with
    `REFUSED -- no tracked Markdown was read ...` and exit 2. The empty list is
    never mistaken for a clean tree.

    `check=True` made `git ls-files` failing OUTSIDE a repo raise
    CalledProcessError, and `scan()` and `named_paths()` both call this first
    -- so the process died on a traceback before `gitignored`'s documented
    fail-soft path could ever run. The docstring above promised graceful
    degradation the tool did not have.

    `-z` for the same reason as above: `.split()` turned a tracked
    `my notes.md` into two names that both fail to open, so that file's prose
    was never scanned and no dangling path inside it could ever be caught.

    Bytes plus a deliberate decode, because `-z` alone does not finish the job.
    Every subprocess text mode turns on universal newlines and rewrites each CR
    byte to LF, and `subprocess` has no `newline=` knob to switch it off.
    MEASURED 2026-08-30: two tracked files `docs/x\\r\\ny.md` and `docs/x\\ny.md`
    come back as two names in bytes mode and as ONE under `text=True`. The
    mistranslated name opens nothing, so `scan()` skips it and the same coverage
    is lost that the paragraph above is about.
    """
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", "*.md"], cwd=root, capture_output=True,
        )
    except OSError as exc:
        print(f"warning: could not run `git ls-files` in {root} ({exc}); "
              f"no Markdown was scanned", file=sys.stderr)
        return []
    if out.returncode != 0:
        detail = out.stderr.decode("utf-8", "replace").strip()[:200]
        print(f"warning: `git ls-files` failed in {root} ({detail}); "
              f"no Markdown was scanned", file=sys.stderr)
        return []
    decoded = out.stdout.decode("utf-8", "surrogateescape")
    names = [f for f in decoded.split("\0") if f]
    # official/overlay/ and official/template-overlay/ are laid over the HEADING
    # OS Official engine and data-template trees by scripts/official-build.py, so
    # their paths name files of THOSE trees (its docs/ guides come from the data
    # repository, `config/data-paths.yaml` is the template's), not of this
    # checkout. Locally the template's names resolve against the data root and
    # pass; in CI there is no data root, so they read dangling.
    return [f for f in names if f not in _SKIP_FILES
            and not f.startswith(("official/overlay/", "official/template-overlay/"))]


class ScanResult(NamedTuple):
    """A scan, plus the size of the corpus that produced it.

    The counts are not diagnostics. `dangling` is empty both when the prose is
    clean and when the scanner inspected nothing, and those two states have to
    be told apart before anything prints OK. `files_read` and `in_scope` are
    what tells them apart, so they travel with the result rather than being
    recomputed by a caller that might forget to ask.
    """

    dangling: dict[str, list[tuple[str, int]]]
    files_read: int
    references_seen: int
    in_scope: int


def scan_scoped(root: Path) -> ScanResult:
    """Scan `root`'s tracked Markdown, carrying the corpus size out with the hits.

    The routing filter runs BEFORE the existence check, which is the reverse of
    the original order. It has to: `in_scope` must count every reference this
    scanner was willing to judge, not only the ones that turned out to be
    missing, or a clean tree would look exactly like a disabled one.
    """
    hits: dict[str, list[tuple[str, int]]] = {}
    files_read = references_seen = in_scope = 0
    for rel in tracked_markdown(root):
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"{YELLOW}skipped {rel}: {exc}{RESET}", file=sys.stderr)
            continue
        files_read += 1
        for lineno, line in enumerate(text.splitlines(), 1):
            for path in _PATH.findall(line):
                references_seen += 1
                if not in_engine_scope(path):
                    continue  # lives in an overlay this tool cannot see
                in_scope += 1
                if (root / path).exists():
                    continue
                hits.setdefault(path, []).append((rel, lineno))
    for path in gitignored(root, sorted(hits)):
        hits.pop(path, None)
    return ScanResult(hits, files_read, references_seen, in_scope)


def scan(root: Path) -> dict[str, list[tuple[str, int]]]:
    """Return {dangling in-scope path: [(file, lineno), ...]}.

    The dangling half of `scan_scoped()`, kept because callers that only want
    the findings should not have to unpack a result they ignore. Anything that
    decides whether to PRINT A PASS must call `scan_scoped()` instead, so the
    corpus counts are in front of it.
    """
    return scan_scoped(root).dangling


# ============================================================
# Coverage: which engine code no prose describes
# ============================================================
#
# A report over the extraction, not a persisted edge table: measured 2026-08-21,
# a table would answer nothing grep does not, and most of its rows were archive
# mentions (operator approved the reduction that day).

# Prose that MENTIONS a path is not prose that DOCUMENTS it. A handoff summary
# quoting a filename does not make that file documented, so these trees are read
# for reporting and excluded from the "is it documented" verdict.
_ARCHIVE_PREFIXES = (
    "outputs/",            # leak-guard: ok (prefix match on a repo-relative string)
    "plans/archive/",      # leak-guard: ok (prefix match on a repo-relative string)
    "chronicle/",          # leak-guard: ok (prefix match on a repo-relative string)
    "docs/superpowers/",  # leak-guard: ok (prefix match on a repo-relative string)
    "threads/",            # leak-guard: ok (prefix match on a repo-relative string)
)


@functools.lru_cache(maxsize=None)
def archive_prefixes(root: Path) -> tuple[str, ...]:
    """`_ARCHIVE_PREFIXES` in the spellings THIS root actually uses.

    The literals are the pre-move spellings; after the moves they matched little,
    and finished plans and closed threads were counted as prose that DOCUMENTS
    the code they merely mention (2026-09-18: 84 engine scripts wrongly called
    documented). So the homes those prefixes became are added from two registry
    GROUPS, `output-dirs` and `prose-archives`, which can fan one prefix out to
    many homes. Not `data_path`: a name answers one address, and what leaves here
    is a tuple of string prefixes for `startswith`, never a path anything opens.

    Only ever ADDS spellings: the literals stay first for a tree that never
    moved, and a registry lacking a group says so on stderr and keeps them.
    Cached per root because `named_paths` asks once per tracked Markdown file.
    """
    out: list[str] = list(_ARCHIVE_PREFIXES)
    for group in ("output-dirs", "prose-archives"):
        try:
            found = homes(group, data_root=root, existing=False)
        except DataPathError as exc:
            print(f"[check-path-references] registry group {group!r} unavailable "
                  f"({exc}); archive prefixes from the literals alone",
                  file=sys.stderr)
            continue
        for home in found:
            try:
                rel = home.relative_to(root).as_posix() + "/"
            except ValueError:
                continue
            if rel not in out:
                out.append(rel)
    return tuple(out)

# What "documented" is claimed ABOUT. Engine Python only: these are the files the
# documentation-propagation rule governs, and the only tree present in every
# clone. Nothing here says anything about .claude/ content or the overlay's code.
_CODE_GLOB = "scripts/**/*.py"


def code_files(root: Path) -> tuple[list[str], int]:
    """(documentable engine Python paths, count of package markers dropped).

    `__init__.py` is a package marker, not a unit anybody documents by name, so
    counting it as undocumented would put permanent noise in the report. The drop
    is RETURNED rather than swallowed, because a narrowed check that prints like a
    complete one is the defect `.claude/rules/scope-claims.md` exists to stop.
    """
    # `.as_posix()`, not `str()`. The keys these are compared against come from
    # the prose regex, whose character class admits `/` and not `\`, so on a
    # Windows checkout every native-separator path failed the `f not in named`
    # test and the report claimed 100% of engine code was undocumented. The
    # sibling `checkpoint-paths.py` already uses `.as_posix()` for repo-relative
    # strings for this reason.
    every = [p.relative_to(root).as_posix() for p in root.glob(_CODE_GLOB)
             if "__pycache__" not in p.parts]
    keep = sorted(f for f in every if not f.endswith("__init__.py"))
    return keep, len(every) - len(keep)


def named_paths(root: Path, *, skip_archives: bool) -> dict[str, list[tuple[str, int]]]:
    """Every repo path this root's tracked Markdown names -> where it is named."""
    found: dict[str, list[tuple[str, int]]] = {}
    for rel in tracked_markdown(root):
        if skip_archives and rel.startswith(archive_prefixes(root)):
            continue
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for path in _PATH.findall(line):
                found.setdefault(path, []).append((rel, lineno))
    return found


def coverage(root: Path, data_root: Path | None) -> dict:
    """Which engine code files no non-archive prose names.

    `data_root` is the private overlay when it is on disk and None otherwise. Its
    absence NARROWS the claim rather than failing: a public clone has no overlay,
    so a file documented only there would read as undocumented. The return value
    carries `overlay_scanned` so the caller can say which claim it is making
    (`.claude/rules/scope-claims.md`).
    """
    named = named_paths(root, skip_archives=True)
    if data_root is not None:
        for path, sites in named_paths(data_root, skip_archives=True).items():
            named.setdefault(path, []).extend(sites)
    files, package_markers = code_files(root)
    undocumented = [f for f in files if f not in named]
    return {
        "code_files": len(files),
        "package_markers_skipped": package_markers,
        "undocumented": undocumented,
        "overlay_scanned": data_root is not None,
    }


def refuse(reason: str, *, as_json: bool) -> int:
    """Print a refusal in the caller's chosen format and return exit code 2.

    `--json` gets an object carrying `error`, not a bare stderr line: a machine
    consumer that only parses stdout would otherwise read the absence of
    findings as an absence of problems, which is the same fail-open this whole
    change is about. The human path writes to stderr so it survives a pipe.
    """
    if as_json:
        print(json.dumps({"error": reason, "dangling": None, "new": None}, indent=2))
    else:
        print(f"{RED}{BOLD}REFUSED{RESET} -- {reason}", file=sys.stderr)
    return 2


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="exit 1 on a dangling path not in BASELINE")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--coverage", action="store_true",
                    help="report engine code that no non-archive prose names (advisory)")
    args = ap.parse_args()
    if args.coverage and args.check:
        # Refused, not ignored. The coverage branch returns before `scan()` ever
        # runs, so `--coverage --check` exited 0 while no dangling-path check
        # had happened - a green result standing in for a check nobody made.
        # The two flags answer different questions and neither subsumes the
        # other, so the caller picks one.
        ap.error("--coverage and --check answer different questions; "
                 "run them separately")

    root = get_workspace_root()

    if args.coverage:
        overlay = get_data_root()
        if data_root_is_demo() or not (overlay / ".git").exists():
            overlay = None
        cov = coverage(root, overlay)
        if args.json:
            print(json.dumps(cov, indent=2))
            return 0
        scanned = "engine + private overlay" if cov["overlay_scanned"] else "engine ONLY"
        # The prefixes ACTUALLY used, per root, not the pre-move literals. Since
        # 2026-09-18 `archive_prefixes` adds each legacy tail's post-move homes,
        # so printing `_ARCHIVE_PREFIXES` here would name five while the run
        # excluded more -- a narrowed sentence about a wider method, which is
        # the shape `.claude/rules/scope-claims.md` forbids in the other
        # direction too.
        used = list(archive_prefixes(root))
        if overlay is not None:
            used += [p for p in archive_prefixes(overlay) if p not in used]
        print(f"{BOLD}{CYAN}Documentation coverage of engine code (advisory){RESET}")
        print(f"{GRAY}Prose sources: {scanned}. Archive trees excluded "
              f"({', '.join(used)}) -- a handoff that mentions a path")
        print(f"does not document it. Claim is about {_CODE_GLOB} and nothing else.{RESET}")
        if not cov["overlay_scanned"]:
            print(f"{YELLOW}The overlay is absent, so a file documented only there "
                  f"reads as undocumented here.{RESET}")
        n = len(cov["undocumented"])
        print(f"\n{BOLD}{n}{RESET} of {cov['code_files']} named in no prose "
              f"{GRAY}({cov['package_markers_skipped']} __init__.py package markers "
              f"not counted){RESET}:")
        for f in cov["undocumented"]:
            print(f"  {GRAY}{f}{RESET}")
        return 0
    # Every refusal below exits 2, never 0 and never 1. 0 is "the prose is
    # clean" and 1 is "the prose names something missing"; a scanner that
    # inspected nothing has established neither, and borrowing either code
    # hands a machine consumer a verdict no measurement backs.
    try:
        require_routing_map(root)
    except ScopeCollapsed as exc:
        return refuse(str(exc), as_json=args.json)

    result = scan_scoped(root)
    if result.files_read == 0:
        return refuse(
            f"no tracked Markdown was read under {root}, so nothing was "
            f"checked. A pass over an empty corpus is not a pass.",
            as_json=args.json)
    if result.in_scope == 0:
        return refuse(
            f"{result.files_read} file(s) were read and "
            f"{result.references_seen} path reference(s) extracted, but every "
            f"one was excluded as overlay-owned, so nothing was checked. Look "
            f"at {root / ROUTING_MAP_REL}: a rule that covers the whole tree "
            f"silences this scanner.",
            as_json=args.json)

    found = result.dangling
    new = {p: sites for p, sites in found.items() if p not in BASELINE}

    if args.json:
        print(json.dumps(
            {"dangling": dict(sorted(found.items())),
             "new": sorted(new),
             # The corpus this verdict rests on, published rather than left to
             # be inferred. A machine consumer reading `"new": []` cannot
             # otherwise tell a clean tree from a scan that inspected nothing,
             # and that is exactly the confusion this scanner shipped with.
             "files_read": result.files_read,
             "references_seen": result.references_seen,
             "in_scope": result.in_scope,
             "baseline": BASELINE},
            indent=2,
        ))
    else:
        print(f"{BOLD}{CYAN}Engine prose path references (advisory){RESET}")
        print(f"{GRAY}{result.in_scope} of {result.references_seen} path reference(s) in "
              f"{result.files_read} tracked Markdown file(s), checked against the engine tree.")
        print(f"The rest are excluded by an explicit routing rule -- the overlay may be absent.{RESET}\n")
        for path, sites in sorted(found.items()):
            reason = BASELINE.get(path)
            tag = f"{GREEN}baseline: {reason}{RESET}" if reason else f"{RED}NEW{RESET}"
            print(f"{BOLD}{path}{RESET} [{len(sites)}] {tag}")
            for rel, lineno in sites[:5]:
                print(f"  {GRAY}{rel}:{lineno}{RESET}")
        stale = sorted(p for p in BASELINE if p not in found)
        if stale:
            print(f"\n{YELLOW}Baseline entries no longer found (drop them):{RESET}")
            for p in stale:
                print(f"  {p}")

    if args.check:
        if new:
            print(f"\n{RED}{BOLD}FAIL{RESET} -- prose names {len(new)} path(s) that do not exist:",
                  file=sys.stderr)
            for path, sites in sorted(new.items()):
                where = ", ".join(f"{r}:{n}" for r, n in sites[:3])
                print(f"  {RED}{path}{RESET}  ({where})", file=sys.stderr)
            print(f"{YELLOW}Fix the path, or add it to BASELINE with the reason it should not exist.{RESET}",
                  file=sys.stderr)
            return 1
        print(f"\n{GREEN}OK{RESET} -- no new dangling path references.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
