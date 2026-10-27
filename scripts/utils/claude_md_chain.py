#!/usr/bin/env python3
"""The chain of `@` imports rooted at a CLAUDE.md-style file.

An instruction file is not one file. The harness follows `@path` tokens inside
it, and the files those name, and so on. Two places in this workspace have to
agree about that chain and used to answer it with one `read_text()` each:

  - `.claude/hooks/session-start.py` injects the private operational layer into
    a YARD, where the parent-relative import in `CLAUDE.md` names a path that
    does not exist. It read ONE file, so an `@` inside that file arrived in the
    session as the literal text `@CLAUDE.private.md` and the rules behind it
    silently did not.
  - `scripts/utils/memory_health.py` asks whether a policy file is reachable
    from `CLAUDE.md` before it will accept a shrunken memory index.

Two copies of a traversal are two answers, and the second copy is the one that
stops being fixed, so the traversal lives here once.

WHAT THIS MIRRORS, AND WHERE IT WAS READ. Claude Code 2.1.277, the bundle at
`~/.local/share/claude/versions/2.1.277`, read 2026-09-20. These are VENDOR
INTERNALS, not a published API: nothing promises they are stable between
versions, and a mismatch means this file is out of date rather than the harness
being wrong. The four facts taken from it, each verbatim from the bundle:

  1. The token regex is `/(?:^|\\s)@((?:[^\\s\\\\]|\\\\ )+)/g`. An `@` at the
     start of the text or preceded by whitespace; the path runs to the next
     whitespace; `\\ ` is an escaped space.
  2. `let L=O.indexOf("#"); if(L!==-1)O=O.substring(0,L)` - a `#` fragment is
     cut off the path.
  3. The acceptance test, which is what keeps an email address out:
     `O.startsWith("./")||O.startsWith("~/")||O.startsWith("/")&&O!=="/"||
      !O.startsWith("@")&&!O.match(/^[#%^&*()]+/)&&O.match(/^[a-zA-Z0-9._-]/)`.
  4. `if(y.type==="code"||y.type==="codespan")continue` - a fenced block and an
     inline span are NOT scanned, and an HTML comment is stripped before the
     rest of the `html` token is. A hook that read those as imports would inject
     files a real session never sees, which is the same defect pointing the
     other way.

And two constants, also read there: `var Lyr=5` bounds the depth, tested as
`s>=Lyr` with the root called at `s=0`, so the root plus four hops; and
`bse=4194304`, above which a file is skipped whole.

WHAT THIS DELIBERATELY DOES NOT MIRROR, because modelling it here would mean
this file answering a question it cannot see:

  - `claudeMdExcludes`, the user's glob list of instruction files to drop. It is
    read from settings the hook has no business parsing, and it is empty on this
    workspace.
  - The external-include approval. At depth > 0 the harness skips an import that
    resolves OUTSIDE the project unless `hasClaudeMdExternalIncludesApproved` is
    set for that project in `~/.claude.json`. That flag is per project and
    per machine, and `.claude/hooks/session-start.py` already carries the
    measurement (2026-09-09) that reading it wrong is how a scratch replica
    "proved" the main clone was broken. So this resolver reports the chain as
    written and leaves that gate to the harness.
  - The markdown tokenizer. Fences, inline spans and HTML comments are removed
    textually here rather than by parsing; see `_strip_uninterpreted`.

Nothing here writes. Nothing here raises: an import that cannot be read comes
back as a `ChainProblem` with a reason, because an instruction layer that
silently did not arrive is the defect both callers exist to end.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# `Lyr` in the bundle. The root is visited at depth 0 and a child is refused at
# `depth >= HARNESS_MAX_DEPTH`, so the root plus four hops below it.
HARNESS_MAX_DEPTH = 5

# `bse` in the bundle: a file larger than this is skipped whole, imports and
# all. The neighbouring 40_000-character threshold is NOT modelled, because it
# only prints a warning row and the file is still injected in full.
HARNESS_FILE_BYTE_LIMIT = 4_194_304

# Fact 1 above. `^` without `re.MULTILINE` is the start of the whole text, which
# is what the JS regex means with no `m` flag; a newline before the `@` is
# already covered by `\s`.
_IMPORT_RE = re.compile(r"(?:^|(?<=\s))@((?:[^\s\\]|\\ )+)")

_FENCE_RE = re.compile(r"^(`{3,}|~{3,})")
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
# A code span is a backtick run closed by a run of the same length. Non-greedy,
# with the opening run back-referenced, which is the CommonMark rule and is
# enough once the fenced blocks above are already gone.
_CODESPAN_RE = re.compile(r"(?P<ticks>`+).+?(?P=ticks)", re.DOTALL)

_FRAGMENT_START_RE = re.compile(r"^[#%^&*()]+")
_BARE_START_RE = re.compile(r"^[a-zA-Z0-9._-]")


# ---------------------------------------------------------------------------
# WHAT A HOOK CAN DELIVER, WHICH IS NOT WHAT AN INSTRUCTION FILE CAN.
#
# The chain above has no aggregate cap. A HOOK printing the same bytes does,
# and nothing anywhere says so at the moment of the loss.
#
# MEASURED 2026-09-20 in the shipped bundle, Claude Code 2.1.277
# (`~/.local/share/claude/versions/2.1.277`), the same build the four facts
# above were read from. Verbatim, in the chunk that persists hook output:
#
#     var c$=50000,HJr=4000,...,MJr=1073741824,DJr=1e4,vEt=1e4,FQe=1e5;
#     async function hee(e,r,n,{threshold:s=DJr,storageV5:a}={}){
#       if(e.length<=s)return e;
#       let o=await P1(e,`hook-${r}-${n}`,S_(),a); ... }
#     var Ige=2000, O="tool result was not saved";
#     ... let{preview:A,hasMore:k}=mFe(d,Ige);
#
# Three facts, each of which changes what a hook may print:
#
#   1. `e.length` is a JS string length, so the unit is UTF-16 CODE UNITS, the
#      same unit as the memory index's own budget. See `utf16_units`.
#   2. It does not refuse and it does not truncate in place. Over the
#      threshold the WHOLE output is persisted to
#      `<projects dir>/<session id>/tool-results/hook-<id>-<source>.txt` and
#      what reaches the model is a `<persisted-output>` wrapper holding that
#      path and a preview of the first `Ige` characters. The terminal shows
#      nothing about it.
#   3. The threshold is PER HOOK INVOCATION and is not per event, so every
#      section one hook prints shares it.
#
# Confirmed behaviourally the same day in a scratch project whose SessionStart
# hook printed numbered filler lines: at 8,000 bytes a marker in the middle came
# back verbatim, and at 16,000, 24,000 and 40,000 it did not, with the session
# able to walk the numbering from 00000 to 00044 and no further. 45 lines of 44
# bytes is 1,980 characters, which is the preview and not the file.
#
# VENDOR INTERNALS, like everything else here: re-read them from the build in
# use, and treat a mismatch as this file being out of date.
HOOK_STDOUT_UTF16_LIMIT = 10_000
HOOK_PERSISTED_PREVIEW_CHARS = 2_000


def utf16_units(text: str) -> int:
    """Length of `text` in UTF-16 code units: what a JS `String.length` returns,
    and therefore the only count the harness's two budgets are expressed in.

    NOT `len(text.encode())`. That is UTF-8 and OVER-counts every non-ASCII
    character. MEASURED 2026-09-09 on the operator's live memory index: 23108
    UTF-8 bytes against 22732 UTF-16 units, a 376 gap opened by the `·` and `—`
    the index uses as separators. A UTF-8 check trips before the harness does
    and misreports the remaining headroom.

    NOT `len(text)` either. That counts CODE POINTS, and agrees with UTF-16 only
    while the text holds nothing outside the BMP. One emoji in one hook and the
    two answers part company in the UNSAFE direction: Python would say the
    output fits while the harness persists it.

    It lives here rather than in `scripts/utils/memory_health.py`, where it was
    written, because `.claude/hooks/session-start.py` has to measure its own
    output against `HOOK_STDOUT_UTF16_LIMIT` and loads this module already.
    `memory_health` re-exports the name, so both callers count once and alike.
    """
    return sum(1 if ord(c) < 0x10000 else 2 for c in text)


def user_instruction_root() -> Path:
    """The USER-level `CLAUDE.md`, the one root that is not inside a project.

    `CLAUDE_CONFIG_DIR` when set, else `~/.claude`, which is how the CLI itself
    resolves its config home. Verbatim from the 2.1.277 bundle, read
    2026-09-20: `function s(){return process.env.CLAUDE_CONFIG_DIR}` and
    `var we=Jo(()=>(s()??a(g(),".claude")).normalize("NFC"),s)`.

    Read from the ENVIRONMENT on every call rather than captured at import,
    because a test drives a hook as a subprocess with its own value and a
    module-level constant would answer for the operator's real home instead.
    """
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(configured) if configured else Path.home() / ".claude"
    return base / "CLAUDE.md"


@dataclass(frozen=True)
class ChainFile:
    """One file the chain loaded, with the text the harness would inject."""

    path: Path
    depth: int
    parent: Path | None
    text: str


@dataclass(frozen=True)
class ChainProblem:
    """A named import that produced no file, and why.

    `named` is the spelling as it appears in the source, so the operator can
    grep for it; `named_by` is the file that wrote it. Both are needed: the
    spelling alone does not say which of several files carries it.
    """

    named: str
    named_by: Path
    resolved: Path | None
    reason: str

    def describe(self) -> str:
        where = self.named_by.name
        return f"{self.named} named by {where} ({self.reason})"


@dataclass
class Chain:
    """The result of a traversal. Never an exception; read `problems`."""

    root: Path
    files: list[ChainFile] = field(default_factory=list)
    problems: list[ChainProblem] = field(default_factory=list)
    root_readable: bool = False

    @property
    def paths(self) -> list[Path]:
        return [f.path for f in self.files]

    @property
    def text(self) -> str:
        """Every loaded file, in the order the harness would present them."""
        return "\n\n".join(f.text for f in self.files)

    def holds(self, target) -> bool:
        """Whether `target` is one of the files this chain loaded.

        Compared on the RESOLVED path, the same key the traversal dedups on, so
        two spellings of one file answer alike.
        """
        try:
            wanted = Path(target).resolve()
        except (OSError, ValueError):
            return False
        return any(f.path == wanted for f in self.files)


def _strip_uninterpreted(text: str) -> str:
    """Blank out the regions the harness's token walk never scans.

    Fenced blocks first, so a stray backtick inside one cannot pair with a real
    code span outside it. Then HTML comments, then inline spans. Each region is
    replaced by whitespace rather than deleted, because the import regex needs
    an `@` to be preceded by whitespace and joining two lines could manufacture
    one that was never written.
    """
    lines = []
    fence_char = ""
    fence_len = 0
    for line in text.split("\n"):
        stripped = line.lstrip(" ")
        indent = len(line) - len(stripped)
        match = _FENCE_RE.match(stripped) if indent <= 3 else None
        if not fence_char:
            if match:
                fence_char = match.group(1)[0]
                fence_len = len(match.group(1))
                lines.append("")
                continue
            lines.append(line)
            continue
        # Inside a fence: only a run of the SAME character, at least as long as
        # the opener, closes it.
        if match and match.group(1)[0] == fence_char and len(match.group(1)) >= fence_len:
            fence_char = ""
            fence_len = 0
        lines.append("")
    without_fences = "\n".join(lines)
    without_comments = _HTML_COMMENT_RE.sub(" ", without_fences)
    return _CODESPAN_RE.sub(" ", without_comments)


def _is_import(spelling: str) -> bool:
    """Fact 3 above, transcribed. Keeps `@misha.hanin@headingos.com` out."""
    if spelling.startswith(("./", "~/")):
        return True
    if spelling.startswith("/") and spelling != "/":
        return True
    if spelling.startswith("@"):
        return False
    if _FRAGMENT_START_RE.match(spelling):
        return False
    return bool(_BARE_START_RE.match(spelling))


def import_spellings(text: str) -> list[str]:
    """Every `@` import in `text`, as written, in document order.

    Duplicates are kept: the caller dedups on the RESOLVED path, and two
    spellings that resolve to one file are not the same fact as one spelling
    written twice.
    """
    found = []
    for match in _IMPORT_RE.finditer(_strip_uninterpreted(text)):
        spelling = match.group(1)
        fragment = spelling.find("#")
        if fragment != -1:
            spelling = spelling[:fragment]
        spelling = spelling.replace("\\ ", " ")
        if spelling and _is_import(spelling):
            found.append(spelling)
    return found


def resolve_import(named: str, containing_dir: Path) -> Path | None:
    """Resolve one import spelling against the directory of its OWN file.

    Not against the project root. MEASURED by the brief this file was written
    for, over a three-level chain in a live session: a relative path inside an
    imported file resolves against the importing file's directory, so a chain
    that walks into a subdirectory keeps walking from there.
    """
    try:
        expanded = os.path.expanduser(named)
        candidate = Path(expanded)
        if not candidate.is_absolute():
            candidate = Path(containing_dir) / candidate
        return candidate.resolve()
    except (OSError, ValueError):
        return None


def named_import_target(root_file, filename) -> Path | None:
    """Resolve the path `root_file`'s first `@` import of `filename` names.

    None when the file names no such import, and None when the file cannot be
    read: both mean "nothing here points at it". The spelling is taken from the
    same parser the traversal uses, so an `@` inside a fenced block or an inline
    span is not an import here either.

    Separate from `resolve_chain` because it answers a different question. The
    chain asks what a session would LOAD; this asks what a line NAMES, which is
    the only question answerable about a path that does not exist.
    """
    root_file = Path(root_file)
    try:
        text = root_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for spelling in import_spellings(text):
        if spelling.endswith(str(filename)):
            return resolve_import(spelling, root_file.parent)
    return None


def delivering_roots(project_claude_md, source, *, user_root=None):
    """Every root a session already loads that delivers `source`.

    Returns `[(label, path)]` in the order the roots are asked, empty when
    nothing delivers it. Labels are `"CLAUDE.md"` and `"~/.claude/CLAUDE.md"`.

    THE TWO ROOTS ARE ASKED DIFFERENT QUESTIONS, and the asymmetry is
    deliberate. The project root is asked whether the path its import NAMES
    exists, which is the condition MEASURED 2026-09-08 and unchanged since:
    whether the harness then follows a path out of the project is settled by
    `hasClaudeMdExternalIncludesApproved`, a per-project answer in
    `~/.claude.json` that a caller here cannot read the meaning of and must not
    second-guess. The user-level root is asked whether its chain actually
    REACHES this file, because there is no line in this repository to point at
    and an absolute import either resolves to this file or does not.

    ASKING BOTH IS THE FIX FOR A DUPLICATE, not a belt and braces. MEASURED
    2026-09-20 in the main clone, by asking a live session to count one section
    heading of the policy file: the answer was 2. The harness dedups on the
    resolved path INSIDE one walk and has no set spanning two roots, so a layer
    wired in both places is resident twice and a reader who finds two copies of
    a rule cannot tell they are the same copy. The engine's own `CLAUDE.md` gave
    up its import that day; this still asks it, because another operator's clone
    may wire it and a caller that stopped asking would act on top of their live
    import.

    One function rather than one per caller. `.claude/hooks/session-start.py`
    decides whether to inject from this, and
    `scripts/check-operational-layer-armed.py` decides whether to report the
    clone unarmed from it. Two copies would be two answers to one question, and
    the second copy is the one that stops being fixed.
    """
    source = Path(source)
    roots = []
    project_claude_md = Path(project_claude_md)
    target = named_import_target(project_claude_md, source.name)
    try:
        if target is not None and target.is_file():
            roots.append(("CLAUDE.md", project_claude_md))
    except OSError:
        pass

    root = Path(user_root) if user_root is not None else user_instruction_root()
    try:
        readable = root.is_file()
    except OSError:
        readable = False
    if readable and resolve_chain(root).holds(source):
        roots.append(("~/.claude/CLAUDE.md", root))
    return roots


def resolve_chain(root, *, max_depth: int = HARNESS_MAX_DEPTH) -> Chain:
    """Load `root` and everything it imports, depth-first, parent before child.

    `max_depth` is the number of LEVELS, with the root at level 0, so the
    harness's own bound is `HARNESS_MAX_DEPTH` and a caller whose root already
    sits one hop down the harness's chain passes one less. A child refused for
    depth is recorded as a problem rather than dropped: the harness drops it
    silently, and a hook that did the same would be the silence this exists to
    end.

    A file is loaded once. The key is its resolved path, so a second spelling of
    a file already in the chain is a no-op rather than a duplicate or a cycle.
    """
    root_path = Path(root).resolve()
    chain = Chain(root=root_path)
    seen: set[Path] = set()

    def visit(path: Path, depth: int, parent: Path | None, named: str) -> None:
        if path in seen:
            return
        if depth >= max_depth:
            if parent is not None:
                chain.problems.append(ChainProblem(
                    named, parent, path,
                    f"deeper than the harness follows ({max_depth} levels from "
                    f"the root of this chain)"))
            return
        seen.add(path)
        try:
            if not path.is_file():
                raise FileNotFoundError(f"no file at {path}")
            size = path.stat().st_size
            if size > HARNESS_FILE_BYTE_LIMIT:
                raise ValueError(
                    f"{size} bytes, over the harness's "
                    f"{HARNESS_FILE_BYTE_LIMIT} byte ceiling, so it is skipped "
                    f"whole")
            text = path.read_text(encoding="utf-8")
        except (OSError, ValueError) as exc:
            chain.problems.append(ChainProblem(
                named, parent if parent is not None else path, path,
                f"{type(exc).__name__}: {exc}"))
            return
        if parent is None:
            chain.root_readable = True
        chain.files.append(ChainFile(path, depth, parent, text))
        for spelling in import_spellings(text):
            child = resolve_import(spelling, path.parent)
            if child is None:
                chain.problems.append(ChainProblem(
                    spelling, path, None, "the path could not be resolved"))
                continue
            if child.suffix.lower() != ".md":
                # The harness resolves it and then fails to read it as an
                # instruction file. Naming it here is the difference between an
                # operator who can see the typo and one who cannot.
                chain.problems.append(ChainProblem(
                    spelling, path, child, "not a .md file"))
                continue
            visit(child, depth + 1, path, spelling)

    visit(root_path, 0, None, str(root_path))
    return chain
