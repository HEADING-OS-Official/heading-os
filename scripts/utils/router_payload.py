#!/usr/bin/env python3
"""The exact strings the router-accuracy judge is sent, in one place.

A checker that rebuilds an outbound payload from its own idea of the sources is
green forever while the sender quietly starts sending something else. That is not
a hypothetical: the first draft of the egress-proof slice modelled the payload as
one concatenation of the source files, and the council pointed out that the wire
carries something different in shape. `judge_query` sends a SYSTEM prompt built
from a judge preamble, the target skill's description and the routing rules, and
then ONE user message per trigger case. A scan of the concatenation would have
missed the preamble and every user message while reporting the payload clean.

So the sender and the checker read from the same functions here. `skill-trigger-
test.py` cannot be imported (a hyphen is not a Python identifier), which is why
the shared half lives in a module and not in the script.

Everything below resolves from the ENGINE root. That is the claim the egress
proof rests on. It was asserted against resolved paths rather than against this
sentence, until the 2026-09-21 test-suite split; now this sentence is all there
is, so resolve the paths yourself before trusting it.

Consumed by: scripts/skill-trigger-test.py, scripts/router-accuracy-nightly.py,
scripts/generate-skill-router.py (OVERLAP_CLUSTERS only).
"""

from __future__ import annotations

import errno
import json
import os
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from scripts.utils.markdown import FM_OK, parse_frontmatter_strict, split_frontmatter
from scripts.utils.workspace import get_workspace_root

ROOT = get_workspace_root()
SKILLS_DIR = ROOT / ".claude" / "skills"
ROUTER_RULE = ROOT / ".claude" / "rules" / "skill-router.md"
CATEGORY_DETAIL_DIR = ROOT / "reference" / "skill-router"

JUDGE_INSTRUCTION = (
    "You are a routing oracle for a Claude Code workspace. Below are the workspace's "
    "skill-routing rules. Given a user message and a TARGET skill, decide whether the "
    "rules would route that message to the TARGET skill (as its primary skill). "
    "Judge strictly by the rules and the target skill's own trigger description "
    "— not by what you personally think is reasonable.\n\n"
    "Reply with ONLY a compact JSON object, no prose:\n"
    '{"routes_to_target": true|false, "skill": "<skill you think fires, or none>", '
    '"reason": "<one short clause>"}'
)

# The skill families whose descriptions overlap, where a live session most often
# picks the wrong sibling. ONE copy: the competitive mode of
# `skill-trigger-test.py` scores per cluster from it, and `generate-skill-router.py`
# renders the always-on collision map from it. A skill may sit in two clusters
# (corporate-letter is in both `email` and `corporate`); order inside a cluster is
# the order the collision map prints.
OVERLAP_CLUSTERS: dict[str, tuple[str, ...]] = {
    "recall": ("recall", "census", "odin", "zk"),
    "email": ("email-draft", "email-respond", "follow-up", "ceo-to-ceo",
              "corporate-letter"),
    "handover": ("handover", "clear-handover", "second-wind"),
    "status": ("dashboard", "next", "prime"),
    "intel": ("osint", "competitor-intel", "market-brief", "meeting-prep"),
    "corporate": ("corporate-letter", "proposal", "partnership-doc",
                  "official-doc", "xpager"),
}

COMPETITIVE_INSTRUCTION = (
    "You route a user message to at most one skill. Answer with the JSON object only."
)


# ============================================================
# Sources
# ============================================================


def router_rules_text() -> str:
    """The router rule text PLUS the per-category detail files.

    F-5.2 moved the exclusions/compound columns out of the always-on router rule
    into reference/skill-router/<category>.md. The judge builds its negative test
    cases from the documented exclusions, so it must see them: concatenate every
    detail file under a clear delimiter. Degrades to the rule alone if the detail
    dir is absent.
    """
    parts = [ROUTER_RULE.read_text(encoding="utf-8")]
    if CATEGORY_DETAIL_DIR.exists():
        for detail in sorted(CATEGORY_DETAIL_DIR.glob("*.md")):
            # Retried once, then REFUSED, and not the skip-and-warn of
            # `scripts.utils.repo_files.read_sources`.
            #
            # The absent-directory branch one line up is not a precedent for
            # skipping a file here. That one is a DECLARED degradation, decided
            # once, on a condition a reader can see: a public clone has no
            # detail directory and the judge is told so. A file that disappears
            # mid-glob produces an UNDECLARED partial payload -- one category's
            # exclusions silently missing from the system prompt -- and this
            # function's whole reason for existing is that "a checker that
            # rebuilds an outbound payload from its own idea of the sources is
            # green forever while the sender quietly starts sending something
            # else." Two calls straddling the race return different strings, so
            # an egress proof would certify a payload that was
            # never sent, and the router-accuracy percentage would move for a
            # reason no reader could reconstruct.
            #
            # The retry recovers a writer's unlink-and-rewrite window and
            # nothing else; a file that is genuinely gone is still gone on the
            # second look.
            try:
                body = detail.read_text(encoding="utf-8")
            except FileNotFoundError:
                try:
                    body = detail.read_text(encoding="utf-8")
                except FileNotFoundError as exc:
                    raise RuntimeError(
                        f"{detail} vanished between the walk and the read. The "
                        f"judge payload would silently lose that category's "
                        f"exclusions, so neither the accuracy figure nor the "
                        f"egress proof would be over the payload actually sent. "
                        f"Re-run once the tree is quiet."
                    ) from exc
            parts.append(
                f"\n\n=== {detail.stem.upper()} DETAIL (exclusions) ===\n"
                f"{body}"
            )
    return "".join(parts)


def load_triggers(skill_dir: Path) -> list[dict]:
    """Return the list of trigger cases for a skill, or [] if it has none."""
    path = skill_dir / "triggers.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path} must be a JSON array of cases")
    return data


def load_skill_description(skill_dir: Path) -> str:
    """Return the `description` frontmatter field of a skill's SKILL.md (best-effort).

    The fences come from the shared splitter. `text.find("\\n---", 3)` sat here
    and took any line merely BEGINNING with three dashes as the close, and
    `startswith("---")` took any line beginning with them as the open. MEASURED
    2026-08-28 against the shared splitter over six documents: a SKILL.md opening
    `---extra` was read here and REFUSED there, so the judge payload was built
    from a file the rest of the engine treats as having no frontmatter.

    The line scan below stays: it deliberately reads the RAW folded scalar rather
    than a YAML-parsed one, so the judge sees the author's own wording.
    """
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return ""
    return description_from_text(skill_md.read_text(encoding="utf-8"))


def description_from_text(text: str) -> str:
    """The raw `description` field of SKILL.md TEXT already read; see
    `load_skill_description`. Split out so the competitive snapshot parses the
    very bytes it captured instead of reading the file a second time."""
    fm, _body, kind = split_frontmatter(text)
    if fm is None or kind != FM_OK:
        return ""
    # Capture `description:` possibly spanning until the next top-level key.
    lines = fm.splitlines()
    desc_lines: list[str] = []
    capturing = False
    for line in lines:
        if line.startswith("description:"):
            capturing = True
            desc_lines.append(line.split(":", 1)[1].strip())
            continue
        if capturing:
            # Continuation lines are indented; a new top-level key ends the field.
            if line and not line[0].isspace():
                break
            desc_lines.append(line.strip())
    return " ".join(d for d in desc_lines if d).strip()


class InvalidSkillMetadata(ValueError):
    """A SKILL.md exists and its frontmatter cannot be read as a mapping, or its
    `disable-model-invocation` is not a boolean."""


class ProvenanceError(RuntimeError):
    """The committed state of a payload source could not be established."""


def read_no_follow(path: Path) -> bytes:
    """The bytes of `path`, read without following a symlink anywhere below ROOT.

    Walks from ROOT one component at a time with `O_NOFOLLOW` relative to the
    parent's descriptor, so the file read is the file checked: a symlink swapped
    in or out after a separate `is_symlink()` test cannot change what is read.
    Raises ProvenanceError for a path outside ROOT, a symlink at any component,
    or something that is not a regular file. Other OSErrors propagate.
    """
    if not path.is_relative_to(ROOT):
        raise ProvenanceError(f"{path} is outside this checkout ({ROOT})")
    parts = path.relative_to(ROOT).parts
    if ".." in parts:
        raise ProvenanceError(f"{path} has a '..' component")
    fd = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            flags = os.O_RDONLY | os.O_NOFOLLOW | (os.O_NONBLOCK if last else os.O_DIRECTORY)
            try:
                nfd = os.open(part, flags, dir_fd=fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise ProvenanceError(f"{path} is or lies under a symlink") from exc
                raise
            os.close(fd)
            fd = nfd
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ProvenanceError(f"{path} is not a regular file")
        with os.fdopen(fd, "rb") as fh:
            fd = -1
            return fh.read()
    finally:
        if fd >= 0:
            os.close(fd)


def git_run(*args: str) -> subprocess.CompletedProcess:
    """`git <args>` run on THIS checkout; a git that cannot start is a
    ProvenanceError.

    The environment is copied with every `GIT_*` variable removed, so an
    inherited `GIT_DIR` or `GIT_WORK_TREE` cannot point the check at another
    repository; `cwd=ROOT` then decides which one git opens.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        return subprocess.run(["git", *args], cwd=str(ROOT), env=env,
                              capture_output=True, check=False)
    except OSError as exc:
        raise ProvenanceError(f"git {args[0]} could not start: {exc}") from exc


def git_output(*args: str) -> bytes:
    """Stdout of `git <args>` via `git_run`, or ProvenanceError when git exits
    non-zero: an answer that could not be established is not a clean one."""
    proc = git_run(*args)
    if proc.returncode != 0:
        raise ProvenanceError(
            f"git {' '.join(args[:2])} failed (exit {proc.returncode}): "
            f"{proc.stderr.decode('utf-8', 'replace').strip()[:200]}")
    return proc.stdout


def git_ignored(path: Path) -> bool:
    """Whether git ignores `path` in this checkout (`git check-ignore -q`, via
    `git_run`). Exit 0 is ignored, 1 is not; anything else is a
    ProvenanceError, never a "not ignored"."""
    proc = git_run("check-ignore", "-q", "--", str(path))
    if proc.returncode not in (0, 1):
        raise ProvenanceError(
            f"git check-ignore failed (exit {proc.returncode}): "
            f"{proc.stderr.decode('utf-8', 'replace').strip()[:200]}")
    return proc.returncode == 0


def payload_sources() -> list[Path]:
    """Every file whose bytes reach the judge, resolved.

    Named rather than inferred, and asserted by the contract to live inside the
    engine and outside the private overlay. A source added to the sender without
    being added here would escape the scan, which is why `outbound_texts` below
    is built from the same accessors the sender calls rather than from this list.
    """
    sources = [ROUTER_RULE]
    if CATEGORY_DETAIL_DIR.exists():
        sources.extend(sorted(CATEGORY_DETAIL_DIR.glob("*.md")))
    for skill_dir in sorted(p for p in SKILLS_DIR.iterdir() if p.is_dir()):
        triggers = skill_dir / "triggers.json"
        if not triggers.is_file():
            continue
        sources.append(triggers)
        skill_md = skill_dir / "SKILL.md"
        if skill_md.is_file():
            sources.append(skill_md)
    return sources


# ============================================================
# The wire
# ============================================================


def system_text(skill_name: str, skill_desc: str | None = None,
                rules: str | None = None) -> str:
    """The system prompt sent for `skill_name`, byte for byte.

    `rules` exists so `outbound_texts` can read the rule set ONCE across a
    24-skill run without restating this format string. Restating it would put the
    wire format in two places inside the module built to keep it in one, which is
    the drift this whole file exists to prevent.
    """
    if skill_desc is None:
        skill_desc = load_skill_description(SKILLS_DIR / skill_name)
    desc = skill_desc or "(no description frontmatter found)"
    return (
        f"{JUDGE_INSTRUCTION}\n\n"
        f"=== TARGET SKILL ===\n/{skill_name}\nDescription: {desc}\n\n"
        f"=== WORKSPACE SKILL-ROUTING RULES ===\n"
        f"{router_rules_text() if rules is None else rules}"
    )


def user_text(query: str, target: str) -> str:
    """The user message sent for one trigger case, byte for byte."""
    return (
        f"User message: {query!r}\n"
        f"Does this route to /{target}? Answer with the JSON object only."
    )


def outbound_texts() -> Iterator[str]:
    """Every string a full `--all` run would send: one system prompt per skill
    with a corpus, and one user message per case in it.

    A generator so the caller decides whether to hold the whole run in memory.
    Skills without a corpus are skipped exactly as the runner skips them, so the
    set is what is SENT rather than what could be.
    """
    rules = router_rules_text()
    for skill_dir in sorted(p for p in SKILLS_DIR.iterdir() if p.is_dir()):
        cases = load_triggers(skill_dir)
        if not cases:
            continue
        name = skill_dir.name
        yield system_text(name, load_skill_description(skill_dir), rules)
        for case in cases:
            yield user_text(case["query"], name)


@dataclass(frozen=True)
class CompetitiveSnapshot:
    """Everything a competitive run sends, read ONCE.

    `captured` maps every engine file whose bytes reached the payload to those
    exact bytes: the router under test, the SKILL.md of every listed skill, and
    the triggers.json of every selected skill that contributed cases. The
    prompt, the vocabulary and the cases are all derived from these bytes, and
    the provenance check compares these bytes, not the disk after the fact.
    """
    router_text: str
    listing: tuple[tuple[str, str], ...]
    cases_by_skill: dict[str, list[dict]]
    captured: dict[Path, bytes] = field(repr=False)

    @property
    def vocabulary(self) -> set[str]:
        return {name for name, _desc in self.listing} | {"none"}

    @property
    def system(self) -> str:
        return competitive_system_text(self.router_text, self.listing)


def competitive_snapshot(router_file: Path, skills: list[str]) -> CompetitiveSnapshot:
    """Read the router, every SKILL.md and the selected skills' cases once.

    Every source is read through `read_no_follow`, so a symlink anywhere below
    ROOT raises ProvenanceError at the read itself.

    Raises InvalidSkillMetadata, failing CLOSED, for a SKILL.md whose
    frontmatter does not parse or whose `disable-model-invocation` is not a
    boolean: either one would otherwise decide by accident whether a skill's
    description is sent. Every skill writes the flag as a YAML boolean
    (MEASURED 2026-10-29: 17 true, 1 false).
    """
    captured: dict[Path, bytes] = {}
    raw = read_no_follow(router_file)
    captured[router_file] = raw
    listing: list[tuple[str, str]] = []
    for skill_dir in sorted(SKILLS_DIR.iterdir()):
        skill_md = skill_dir / "SKILL.md"
        if not skill_dir.is_dir() or not skill_md.is_file():
            continue
        body = read_no_follow(skill_md)
        text = body.decode("utf-8")
        data, kind, detail = parse_frontmatter_strict(text)
        if kind != FM_OK or not isinstance(data, dict):
            raise InvalidSkillMetadata(f"{skill_md}: frontmatter {kind}"
                                       + (f": {detail}" if detail else ""))
        flag = data.get("disable-model-invocation", False)
        if not isinstance(flag, bool):
            raise InvalidSkillMetadata(
                f"{skill_md}: disable-model-invocation is {flag!r}, not a boolean")
        if flag:
            continue
        captured[skill_md] = body
        listing.append((skill_dir.name, description_from_text(text)))
    cases_by_skill: dict[str, list[dict]] = {}
    for name in skills:
        path = SKILLS_DIR / name / "triggers.json"
        if not path.is_file():
            continue
        body = read_no_follow(path)
        cases = json.loads(body)
        if not isinstance(cases, list):
            raise ValueError(f"{path} must be a JSON array of cases")
        if cases:
            captured[path] = body
            cases_by_skill[name] = cases
    return CompetitiveSnapshot(raw.decode("utf-8"), tuple(listing),
                               cases_by_skill, captured)


def competitive_system_text(router_text: str,
                            listing: tuple[tuple[str, str], ...]) -> str:
    """The system prompt of a competitive run, byte for byte.

    Reproduces what a live session sees: every model-invocable skill's
    description, one line each in name order, then the router under test. No
    detail file, because a live session does not load one before choosing.
    """
    lines = "\n".join(f"- {name}: {desc}" for name, desc in listing)
    return (
        f"{COMPETITIVE_INSTRUCTION}\n\n{lines}\n\n"
        f"=== WORKSPACE SKILL-ROUTING RULES ===\n{router_text}"
    )


def competitive_user_text(query: str) -> str:
    """The user message of a competitive run for one case, byte for byte."""
    return f"User message: {query!r}"


def provenance_mismatches(captured: dict[Path, bytes]) -> list[str]:
    """Captured sources whose bytes are not the bytes committed at HEAD.

    Returns the repository-relative paths that differ from their HEAD blob or
    are not in HEAD at all (untracked or ignored). RAISES ProvenanceError, which
    no flag overrides, when the answer cannot be established: a git command
    that fails, a source outside this checkout, or a symlink (in the tree on
    disk or as a mode-120000 blob), because a symlink's committed blob says
    nothing about the bytes behind it.

    Bound to the bytes read, not to the disk: `git status` after the read
    certified a router that was restored between read and check while the
    dirty snapshot went out (MEASURED 2026-10-29), and it answered "clean"
    whenever git itself failed.
    """
    rel: dict[Path, str] = {}
    for path in captured:
        # A second line behind `read_no_follow`, which already refused a
        # symlink at the read; this catches one in the tree as it is now.
        if path.is_symlink() or Path(os.path.realpath(path)) != path.absolute():
            raise ProvenanceError(f"{path} is or lies under a symlink")
        if not path.is_relative_to(ROOT):
            raise ProvenanceError(f"{path} is outside this checkout ({ROOT})")
        rel[path] = path.relative_to(ROOT).as_posix()
    # Only git's own newline: a checkout path may end in a space.
    top = git_output("rev-parse", "--show-toplevel").decode(
        "utf-8", "surrogateescape").removesuffix("\n")
    if Path(top).resolve() != ROOT.resolve():
        raise ProvenanceError(f"git opened {top}, not this checkout ({ROOT})")
    # Every ancestor directory too: a symlink committed as a PARENT hides the
    # descendant from ls-tree, which would read as an overridable "not in HEAD".
    ancestors = {"/".join(n.split("/")[:i]) for n in rel.values()
                 for i in range(1, n.count("/") + 1)}
    listed = git_output("ls-tree", "-z", "HEAD", "--", *sorted(ancestors), *rel.values())
    blobs: dict[str, tuple[str, str]] = {}
    for record in listed.decode("utf-8", "surrogateescape").split("\0"):
        if not record:
            continue
        meta, name = record.split("\t", 1)
        mode, _type, sha = meta.split(" ")
        blobs[name] = (mode, sha)
    for name in sorted(ancestors):
        if blobs.get(name, ("",))[0] == "120000":
            raise ProvenanceError(f"{name} is committed as a symlink")
    differ: list[str] = []
    for path, name in rel.items():
        if name not in blobs:
            differ.append(name)
            continue
        mode, sha = blobs[name]
        if mode == "120000":
            raise ProvenanceError(f"{name} is committed as a symlink")
        if git_output("cat-file", "blob", sha) != captured[path]:
            differ.append(name)
    return sorted(differ)


def dirty_sources() -> list[str]:
    """Payload sources git reports as modified, staged, or untracked.

    The egress argument is not "we scanned it" — a denylist knows only the
    entities the overlay happens to carry. It is "this content already passed the
    content-leak wall that decides whether a file may become public, and it has
    not changed since". A source with uncommitted edits breaks the second half,
    so it is named and the proof refuses.

    Returns [] when git cannot answer. That direction is deliberate: the caller
    treats an unbuildable denylist as unverifiable already, and a git failure
    must not become a second, silent way to refuse forever.
    """
    rel = sorted({str(p.relative_to(ROOT)) for p in payload_sources()
                  if p.is_relative_to(ROOT)})
    if not rel:
        return []
    # `-z` and `--no-renames`, because the refusal has to NAME a file the
    # operator can open.
    #
    # MEASURED 2026-08-30, both halves, because only one of them was real:
    #   * The C-quoting IS reachable. Without `-z`, `git status --porcelain`
    #     emits a path needing quoting with its escapes intact - a file named
    #     `od"un.md` arrives as `"ref/od\"un.md"` - and `.strip().strip('"')`
    #     peeled the outer quotes and left the backslash, naming a path that
    #     does not exist. `-z` emits the bytes verbatim and needs no unquoting.
    #   * The rename form is NOT reachable HERE. `git status --porcelain`
    #     does report `R  old -> new`, but only when the pathspec covers BOTH
    #     sides; this call passes the declared sources as explicit paths, and a
    #     pathspec matching one side collapses the record to a plain `D old` or
    #     `A new`. `--no-renames` is kept anyway, so the guarantee holds if the
    #     pathspec ever widens to a directory.
    # The `.strip()` goes with them: it would re-corrupt exactly the
    # leading/trailing-whitespace names `-z` exists to deliver intact.
    #
    # And the output is decoded from BYTES, not read through subprocess text
    # mode, because `-z` reaches only the quoting half. Text mode turns on
    # universal newlines, rewriting every CR byte to LF, and `subprocess` has no
    # `newline=` knob to switch it off. MEASURED 2026-08-30: two tracked files
    # differing only by that byte come back as two records in bytes mode and as
    # one under `text=True`. The refusal has to NAME a file the operator can
    # open, and a translated name is not that file.
    proc = subprocess.run(
        ["git", "status", "--porcelain", "-z", "--no-renames", "--", *rel],
        cwd=str(ROOT), capture_output=True, check=False,
    )
    if proc.returncode != 0:
        return []
    decoded = proc.stdout.decode("utf-8", "surrogateescape")
    dirty: list[str] = []
    for record in decoded.split("\0"):
        if len(record) > 3:
            dirty.append(record[3:])
    return dirty
