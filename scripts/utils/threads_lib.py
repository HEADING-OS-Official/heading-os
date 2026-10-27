#!/usr/bin/env python3
"""Thread file parsing and writing helpers."""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from datetime import date, datetime
import re
import yaml
from scripts.utils.atomic import atomic_write_text
from scripts.utils.markdown import FM_OK, split_frontmatter
from scripts.utils.slugs import transliterate
from scripts.utils.workspace import get_default_tz

REQUIRED_FIELDS = (
    "id", "title", "status", "type", "classification",
    "opened", "last_touched", "links", "tags",
)
VALID_STATUSES = ("active", "on-hold", "closed")
VALID_TYPES = ("business", "personal")
# Every frontmatter key ThreadFile models explicitly. Anything else round-trips
# through `ThreadFile.extra` rather than being dropped on write.
MODELLED_FIELDS = frozenset({
    "id", "title", "status", "type", "classification", "opened", "last_touched",
    "counterparties", "links", "tags", "quiet_until", "do_not_remind",
})


@dataclass
class ThreadFile:
    id: str
    title: str
    status: str
    type: str
    classification: str
    opened: str
    last_touched: str
    counterparties: list[str] = field(default_factory=list)
    links: dict[str, list[str]] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    body: str = ""
    path: Path | None = None
    # ISO date up to and including which this thread must not be surfaced
    # proactively. Optional; absent on almost every thread.
    quiet_until: str | None = None
    # Indefinite freeze: quiet with no end date, lifted only when the operator
    # raises the subject themselves. Distinct from `quiet_until`, which expires.
    do_not_remind: bool = False
    # Frontmatter keys this dataclass does not model, carried verbatim so a
    # rewrite cannot silently delete them. `write_thread_file` used to rebuild
    # frontmatter from a fixed field list, which destroyed every hand-added key
    # on the next `/thread log` -- discovered 2026-08-12 when it ate a freeze
    # flag that had been sitting in a thread for six weeks.
    extra: dict[str, Any] = field(default_factory=dict)


def parse_thread_file(path: Path) -> ThreadFile:
    """Parse a thread markdown file into a ThreadFile dataclass."""
    text = path.read_text(encoding="utf-8")
    # Fences via the shared splitter. `startswith("---\n")` plus
    # `split("---\n", 2)` demanded the fence be exactly four characters, so a
    # thread file whose opening fence carries a trailing space or tab RAISED
    # "missing YAML frontmatter" on a file that is perfectly valid YAML.
    # MEASURED 2026-08-28: `--- ` and `---\t` both raised here and both parsed
    # cleanly through the splitter. This function has 45 callers; the one that
    # catches ValueError is `scan_for_archive`, which then drops the thread from
    # the archive scan without a word, and the other callers get a traceback.
    frontmatter_raw, body, kind = split_frontmatter(text)
    if frontmatter_raw is None or kind != FM_OK:
        raise ValueError(f"{path}: missing YAML frontmatter")
    fm: dict[str, Any] = yaml.safe_load(frontmatter_raw) or {}

    for required in REQUIRED_FIELDS:
        if required not in fm:
            raise ValueError(f"{path}: missing required field '{required}'")
    if fm["status"] not in VALID_STATUSES:
        raise ValueError(f"{path}: invalid status '{fm['status']}'")
    if fm["type"] not in VALID_TYPES:
        raise ValueError(f"{path}: invalid type '{fm['type']}'")
    if fm["id"] != path.stem:
        raise ValueError(
            f"{path}: id {fm['id']!r} does not match filename stem {path.stem!r}",
        )

    # Normalize links: guarantee all four sub-keys exist, even if frontmatter is partial.
    # Defensive copy avoids mutating the YAML-loaded dict in place.
    links = dict(fm.get("links") or {})
    for key in ("crm", "pipeline", "outputs", "knowledge"):
        links.setdefault(key, [])

    return ThreadFile(
        id=fm["id"],
        title=fm["title"],
        status=fm["status"],
        type=fm["type"],
        classification=fm["classification"],
        opened=str(fm["opened"]),
        last_touched=str(fm["last_touched"]),
        counterparties=fm.get("counterparties") or [],
        links=links,
        tags=fm.get("tags") or [],
        body=body.lstrip("\n"),
        path=path,
        quiet_until=str(fm["quiet_until"]) if fm.get("quiet_until") else None,
        do_not_remind=bool(fm.get("do_not_remind")),
        extra={k: v for k, v in fm.items() if k not in MODELLED_FIELDS},
    )


def slugify(text: str) -> str:
    """Convert text to lowercase kebab-case suitable for filenames.

    Dots and whitespace are converted to hyphens (preserves 'acme.io' -> 'acme-io',
    not destructive 'acmeio'). Parens, punctuation, and other non-alphanumeric
    chars are stripped. Multiple hyphens collapse to one.

    Cyrillic is transliterated first, so a Russian title keeps its words instead
    of being erased. A MIXED title was the loss that showed: the live thread
    "Миграция CRM на новый сервер" would carry the id `crm`, five words
    in and one word out. `new_thread_path` still RAISES when the result is empty,
    which is the right answer for an id a person has to read.
    """
    text = transliterate(text).lower()
    # Step 1: dots and whitespace -> hyphen
    text = re.sub(r"[.\s]+", "-", text)
    # Step 2: strip everything that isn't alphanumeric or hyphen
    text = re.sub(r"[^a-z0-9-]", "", text)
    # Step 3: collapse multi-hyphens
    text = re.sub(r"-+", "-", text)
    return text.strip("-")


def new_thread_path(threads_root: Path, type_: str, title: str, date: str, *,
                    home: Path | None = None) -> Path:
    """Build the canonical path for a new thread file.

    `home` is the directory the thread goes in when it is not
    `<threads_root>/<type>`: the business threads' registry name, or one
    account's `account-threads`.
    """
    if type_ not in VALID_TYPES:
        raise ValueError(f"invalid type '{type_}'")
    slug = slugify(title)
    if not slug:
        raise ValueError(
            f"title {title!r} slugifies to empty; provide a title with at least "
            f"one Latin or Cyrillic letter, or a digit",
        )
    # Belt-and-braces: `slugify` already strips parens, so this can only fire if
    # that changes. A paren in the stem breaks every markdown link that names the
    # thread path, in the thread's own `links:` and in anything that quotes it.
    if "(" in slug or ")" in slug:
        raise ValueError(f"slug must not contain parens: {slug!r}")
    return (home if home is not None else threads_root / type_) / f"{date}-{slug}.md"


def write_thread_file(path: Path, thread: ThreadFile) -> None:
    """Write a ThreadFile back to disk with frontmatter + body."""
    fm = {
        "id": thread.id,
        "title": thread.title,
        "status": thread.status,
        "type": thread.type,
        "classification": thread.classification,
        "opened": thread.opened,
        "last_touched": thread.last_touched,
        "counterparties": thread.counterparties,
        "links": thread.links,
        "tags": thread.tags,
    }
    # Emitted only when set, so the field never appears on the threads that have
    # no quiet period. It IS emitted here rather than dropped: the previous
    # fixed-field rebuild silently deleted any freeze flag on the next log.
    if thread.quiet_until:
        fm["quiet_until"] = thread.quiet_until
    if thread.do_not_remind:
        fm["do_not_remind"] = True
    # Unmodelled keys last, and never allowed to overwrite a modelled one.
    for key, value in thread.extra.items():
        if key not in fm:
            fm[key] = value
    fm_yaml = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, default_flow_style=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = thread.body.lstrip("\n")
    atomic_write_text(path, f"---\n{fm_yaml}---\n\n{body}")


# ============================================================
# Quiet periods
# ============================================================

def is_quiet(thread: ThreadFile, today: date) -> bool:
    """Is this thread inside a deliberate quiet period on `today`?

    A quiet thread must not be surfaced proactively -- not in a session-opener
    rollup, /next, /dashboard, /weekly-review, or an ad-hoc "what is open"
    answer. An unset or unparseable `quiet_until` is NOT quiet: a broken date
    must fail toward surfacing the thread, never toward silencing it forever.
    `do_not_remind` is the dateless form, lifted only when the operator raises
    the subject themselves.
    """
    if thread.do_not_remind:
        return True
    if not thread.quiet_until:
        return False
    try:
        return today <= date.fromisoformat(thread.quiet_until)
    except (ValueError, TypeError):
        return False


# The `## Active Threads` block in the auto-memory index and its writer were
# removed 2026-08-27; read the live set with `python scripts/thread.py list`.


# ============================================================
# Archive Scanner
# ============================================================

@dataclass
class ArchiveCandidate:
    path: Path
    action: str  # "archive" | "propose-on-hold" | "quiet-expired"
    reason: str
    type: str = ""  # "business" | "personal": the directory's, not the file's name


# ============================================================
# Where thread files sit
# ============================================================

#: The registry group listing every directory a business thread sits in:
#: `threads-business` and each account's `account-threads` (HOS-234 part 10).
BUSINESS_GROUP = "business-threads"


def business_thread_dirs(threads_root: Path | None = None, *,
                         data_root: Path | None = None) -> list[Path]:
    """Every existing directory holding business threads, deduplicated.

    The registry group `business-threads` first, which walks each account's
    `account-threads` (`scripts/utils/accounts.py`), then `<threads_root>/business`
    when a caller passes the registry root, so a thread left at the pre-move
    address is still seen. A registry without the group is answered with the
    two names it would list, so no account thread drops out on such a tree.
    """
    from scripts.utils.data_homes import homes, resolve_all
    from scripts.utils.data_path import UnknownDataPath
    try:
        dirs = list(homes(BUSINESS_GROUP, data_root=data_root))
    except UnknownDataPath:
        dirs = [h for name in ("threads-business", "account-threads")
                for h in resolve_all(name, data_root) if h.is_dir()]
    if threads_root is not None and (threads_root / "business").is_dir():
        dirs.append(threads_root / "business")
    out: list[Path] = []
    seen: set[Path] = set()
    for d in dirs:
        key = d.resolve()
        if key not in seen and d.is_dir():
            seen.add(key)
            out.append(d)
    return out


def thread_dirs(threads_root: Path, *, data_root: Path | None = None) -> list[tuple[str, Path]]:
    """`(type, directory)` for every directory a thread file sits in, existing only.

    Business threads live in several places (`business_thread_dirs`). Personal
    threads live in ONE home, the registry name `threads-personal` (HOS-235),
    and never under an account; `<threads_root>/personal` is read as well, the
    pre-C3 address, so a thread left there is still listed (the
    personal-threads wall refuses both spellings).
    """
    out = [("business", d) for d in business_thread_dirs(threads_root, data_root=data_root)]
    seen: set[Path] = set()
    for personal in (personal_thread_home(data_root=data_root), threads_root / "personal"):
        if personal.is_dir() and personal.resolve() not in seen:
            seen.add(personal.resolve())
            out.append(("personal", personal))
    return out


def personal_thread_home(*, data_root: Path | None = None) -> Path:
    """Where a new personal thread is written, by the registry name the wall reads.

    Spelled at the call so the data-path gates can read it; it is the same name
    as `PERSONAL_HOME_NAME` in `scripts/utils/personal_paths.py`, which the
    personal-threads wall resolves, and the two must not drift.
    """
    from scripts.utils.data_path import data_path
    return data_path("threads-personal", data_root=data_root)


def scan_for_archive(threads_root: Path, *, dirs: list[tuple[str, Path]],
                     today: date | None = None) -> list[ArchiveCandidate]:
    """Find threads to archive (closed >90 days), propose on-hold for (active
    >60 days), or whose quiet period has run out.

    A thread inside its quiet period is skipped entirely: the 60-day staleness
    nudge is exactly the noise the quiet exists to suppress, and a deliberate
    pause reads as neglect to a date-only check. Once the quiet expires it is
    reported ONCE as `quiet-expired`, which is what closes the loop -- without
    it a freeze would outlive the condition it was set for.
    """
    today = today or datetime.now(get_default_tz()).date()
    candidates: list[ArchiveCandidate] = []
    for type_, type_dir in dirs:
        if not type_dir.is_dir():
            continue
        for f in type_dir.glob("*.md"):
            try:
                t = parse_thread_file(f)
            except (OSError, ValueError, yaml.YAMLError):
                # OSError too. `parse_thread_file` opens with `read_text`, and
                # the glob above is a snapshot: an entry that vanishes, turns
                # unreadable, or is a DIRECTORY named `*.md` raises out of the
                # read, which is in neither of the other two clauses. Measured
                # 2026-08-30 against `threads/business/notafile.md/`:  # leak-guard: ok (measurement: the address as it was on the day measured)
                # IsADirectoryError aborted the whole scan and BOTH type
                # directories returned zero candidates. One bad entry must cost
                # one entry.
                continue
            if is_quiet(t, today):
                continue
            if t.quiet_until:
                candidates.append(ArchiveCandidate(
                    path=f, type=type_, action="quiet-expired",
                    reason=f"quiet period ended {t.quiet_until}; clear it with "
                           f"`thread.py quiet {t.id} --clear`",
                ))
            try:
                last = date.fromisoformat(t.last_touched)
            except (ValueError, TypeError):
                continue
            age = (today - last).days
            if t.status == "closed" and age > 90:
                candidates.append(ArchiveCandidate(
                    path=f, type=type_, action="archive",
                    reason=f"closed {age} days, threshold 90",
                ))
            elif t.status == "active" and age > 60:
                candidates.append(ArchiveCandidate(
                    path=f, type=type_, action="propose-on-hold",
                    reason=f"active but no touch for {age} days, threshold 60",
                ))
    return candidates
