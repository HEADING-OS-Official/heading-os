"""The version detector: it PROPOSES families of document versions, nothing more.

`detect()` reads one `doc_scan.ScanResult` and the registry's replayed `State`
and returns a `Detection`: the `family_proposed` / `member_proposed` events it
would add, plus what it noticed and deliberately did NOT propose (byte-identical
copies, representations of one revision, groups it refused and why). It never
writes anything: no registry event, no cache, no file. The caller (`doc-versions.py scan`) decides
whether to append the events under `version_registry.locked()` and whether to
persist the shingle cache `detect()` hands back.

## Three relations, kept apart

- **Versions**: two files of the same extension, linked by the signals below.
  Only these are ever proposed.
- **Byte-identical copies**: `ScanResult.duplicates`. Reported as `duplicates`
  and never proposed; only the first path of each group (sorted) takes part in
  version detection, so a copy cannot make a version pair look like two.
- **Representations of one revision**: the same stem in the same directory
  with different extensions (a document and its PDF export). Reported as
  `representations`; versions are only ever paired within one extension, so a
  representation pair can never become a proposal.

## The signals, every one of them printed in `reasons`

1. **Normalised name.** The stem, lower-cased, separators folded, with the
   tokens that mark a revision rather than a document dropped: `v2`, `r02`,
   `final`, `draft`, `copy`, `(1)`, a single letter after digits (`02b` reads
   as `02`), and every date or period, which the next signal judges on its
   own. Two names MATCH when their tokens joined without separators are equal,
   so `ab cd` and `abcd` match. The original name is always kept; the
   normalised one is a signal.
2. **Dates in the ORIGINAL names.** A full day date (`20240115`,
   `2024-01-15`, `15-01-2024`) is a SAVE STAMP: two matching names stamped on
   different days are read as one document saved twice, judged on text like
   any matching pair. A coarser period (a year, a month, a quarter, a month
   word beside a year or day) is a REPORTING PERIOD: two names carrying
   different ones are most likely reports for different periods, so such a
   pair needs `DATE_CONFLICT_THRESHOLD` on text. With no text, a pair whose
   periods conflict or whose stamps differ is never linked.
3. **Names that differ.** A pair whose names do not match links only when one
   name's tokens hold all of the other's, the extra tokens carry no
   ALTERNATIVE marker (`ALTERNATIVE_WORDS`, a contract term such as `5yr`, a
   money amount such as `1,5M`), and its text reaches
   `CONTAINED_NAME_THRESHOLD`. Extra `src` / `source` tokens mark the source
   of a built file, which is a representation, never a version. Any other
   difference in the names refuses the pair whatever its text: an alternative
   variant, a sweep step, a working file or a different document can carry
   nearly the same text as its sibling.
4. **Text.** 5-word shingles of `doc_scope.text_for` (the `.md` file itself,
   or a valid `-extract.md` twin), each weighted by inverse document frequency
   over every document with text in this pass, compared by weighted Jaccard.
   A shingle that every document shares (template boilerplate) weighs almost
   nothing next to one only two documents share.
5. **No usable text** (no valid twin, an empty extraction, a text over
   `MAX_TEXT_BYTES`): "comparison unavailable", and the pair stands on the
   name alone.

A pair is linked when its names match, both sit in the SAME directory, it is
not date-conflicted and text is unavailable; or when it passes the name rules
above and its text score reaches its bar: `SAME_NAME_THRESHOLD` for matching
names, `CONTAINED_NAME_THRESHOLD` for names one of which holds the other,
raised to `DATE_CONFLICT_THRESHOLD` when the names carry different periods. A
pair across directories needs text and matching folder names: generic names
such as converter outputs repeat there without being versions. A month word counts as
a period only beside a year or a day number. A text pair scoring from
`NEAR_MISS_FLOOR` up that was not linked, under its bar or refused by a name
rule, is kept in `Detection.near_misses`, so the rules can be tuned in both
directions.

Linked pairs form connected groups. A group whose linked pairs are fewer than
`MIN_GROUP_DENSITY` of all its pairs is a chain (A like B, B like C, C like D)
rather than one document's revisions, and is refused and named in `notes`.

## Candidate pairs, never the whole corpus squared

Pairs come from three kinds of block only: files sharing an extension and a
normalised name in one directory; files with text sharing an extension and one
directory; and files with text sharing an extension and a normalised name
whose DIRECTORIES also carry matching names of at least `MIN_FOLDER_TOKENS`
tokens, anywhere in the corpus (`folder_key`: the directory's normalised
tokens, sorted, so `2024-01-15_tool-x` and `x-tool-v3` match). The third is
how one document's line of revisions is found when each revision was filed in
its own folder and the folders sit far apart. It replaced, on 2026-10-01, the
sibling-directory block of the spec's "same or sibling directory": there the
FILE name is usually a generic role (`new.md`, `current.md`, `README.md`) and
the folder name is the document, so a pair across folders is compared only
when the folders name the same document. A block larger than
`MAX_BLOCK_FILES` is skipped and named in `notes`, never compared pairwise.

## What is never re-proposed

- A family whose every revision lies inside one rejection is dead: it holds
  no files and blocks no overlap, and only the rejection itself governs those
  revisions. A rejected pair is never an edge.
- A file already a member of ANY live family, confirmed or proposed, joins
  nothing new. A new file linked to the members of exactly one CONFIRMED family becomes
  a `member_proposed` for that family; links into a still-proposed family wait
  for the operator to decide that family first.
- A proposal whose set of SHA-256s equals, contains, or is contained in an
  existing family's set is not emitted, so a re-run emits nothing it already
  emitted, whatever the files are now called.
- A rejection says every pair inside it is "not versions of each other". So no
  two files whose SHA-256s lie in one rejected set are ever linked, a proposal
  holding two SHA-256s of one rejected set is refused whole (named in
  `notes`), and a new file is never proposed into a family that holds another
  revision of a rejected set it belongs to. A rejection is keyed by content,
  so renaming the files does not escape it, and neither does a third file
  linked to both, nor a rejected family later shrunk to part of the set.
- Idempotency rests on the SHA-256 sets alone. Family ids are fresh uuid4
  values; `version_registry.replay` refuses a second `family_proposed` for an
  id it already holds.
- Of a group of byte-identical copies, the representative is a path already in
  a family, else one without a copy marker, else the first by name.

## Possible moves

A member whose state is `unavailable` or `missing`, whose recorded bytes now
sit at a path no family knows (the scan's own relocation refused it as
ambiguous), is proposed back into its family as a new member with the reason
"possible move". The operator confirms the right path; nothing is moved.

## The shingle cache

`{rel: [size, mtime_ns, twin_stamp, payload]}`: the file's own size and
mtime, its twin's `[size, mtime_ns]` (or `None`), and either the sorted
shingle hashes or a string naming why there is no text. The twin's stamp is
part of the key beside the controller's (path, size, mtime), because a twin
written after the original would otherwise never be read. A file whose size or
mtime changes while its text is being read is skipped for this pass and not
cached. `load_cache` / `save_cache` read and write it; `save_cache` writes it
atomically through `atomic_write_text`.

The thresholds and markers were set on 2026-10-01 on the tuning half of the
operator's labelled sample of real proposals (HOS-178) and checked on the
other half, never on the invented fixtures of the self-test. The split, the
counts and the held-out result are in the data repository with that issue.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sys
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

_HERE = Path(__file__).resolve()
if str(_HERE.parents[2]) not in sys.path:
    sys.path.insert(0, str(_HERE.parents[2]))

from scripts.utils import doc_scope  # noqa: E402
from scripts.utils import version_registry as vr  # noqa: E402
from scripts.utils.atomic import atomic_write_text  # noqa: E402
from scripts.utils.data_path import data_path  # noqa: E402
from scripts.utils.doc_scan import ScanResult  # noqa: E402

#: Weighted Jaccard a pair with MATCHING names must reach. Was one bar of 0.5
#: for every pair (2026-09-30). Set 2026-10-01 on the tuning half (HOS-178):
#: every matching-name pair the operator labelled there was a version, the
#: lowest of them scored just under 0.4 (revisions saved weeks apart drift),
#: so the bar sits a margin under that.
SAME_NAME_THRESHOLD = 0.35

#: The bar for a pair whose names differ but one holds all the other's tokens
#: (`report` / `report notes added`). Set 2026-10-01 on the tuning half
#: (HOS-178): differing names were the bulk of the false proposals at 0.5,
#: and the contained pairs labelled versions there all scored above 0.95.
CONTAINED_NAME_THRESHOLD = 0.9

#: The bar when the two ORIGINAL names carry different reporting periods
#: (year, month, quarter). 0.9 since 2026-09-30. Day stamps no longer raise
#: it (2026-10-01, HOS-178): in the tuning half most pairs whose names differ
#: only by a day date were labelled versions, saved on different days.
DATE_CONFLICT_THRESHOLD = 0.9

#: Words that, among the tokens one name has and the other lacks, mark an
#: ALTERNATIVE (a variant offered beside the other) rather than a revision.
#: 2026-10-01, HOS-178 tuning half: with/without pairs and a named
#: "variant" beside the plain document were labelled "not versions".
ALTERNATIVE_WORDS = frozenset({"with", "without", "variant", "вариант", "option"})

#: Extra tokens naming the SOURCE of a built file (`page.src.html` beside
#: `page.html`): a representation of one revision, never a version.
#: 2026-10-01, HOS-178 tuning half, labelled "not versions".
SOURCE_WORDS = frozenset({"src", "source"})

#: Words per shingle.
SHINGLE_WORDS = 5

#: A text larger than this is not shingled; the pair falls back to its name.
MAX_TEXT_BYTES = 2_000_000

#: A block (one normalised name, or one directory) larger than this is not
#: compared pairwise, and is named in `Detection.notes`.
MAX_BLOCK_FILES = 250

#: A directory name must carry at least this many tokens to identify one
#: document across folders. 2026-10-01, inferred, not measured: a one-word
#: folder (`documents`, `letters`) names a kind of content, not one document.
MIN_FOLDER_TOKENS = 2

#: A connected group whose linked pairs are fewer than this share of all its
#: pairs is a chain, not a family, and is refused (named in `notes`).
MIN_GROUP_DENSITY = 0.5

#: A text pair scoring at least this but under its threshold is a near miss,
#: kept so the threshold can be tuned in both directions.
NEAR_MISS_FLOOR = 0.3

_STATELESS = frozenset({"unavailable", "missing"})


@dataclass
class Detection:
    events: list[dict] = field(default_factory=list)
    duplicates: list[list[str]] = field(default_factory=list)
    representations: list[list[str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    near_misses: list[tuple[float, str, str, str]] = field(default_factory=list)
    cache: dict[str, list] = field(default_factory=dict)
    with_text: int = 0
    without_text: int = 0


# ============================================================
# Names
# ============================================================

_TOKEN_SPLIT = re.compile(r"[^0-9a-zа-яё]+")
# `r02` joined `v2` on 2026-10-01 (HOS-178 tuning half: `-r01-` / `-r02-`
# estimates labelled versions).
_NOISE = re.compile(r"^(?:v\d+[a-z]?|r\d{1,3}|final|draft|copy)$")
# A contract term among the extra tokens (`5yr`): an alternative offer, not a
# revision. 2026-10-01, HOS-178 tuning half.
_TERM = re.compile(r"^\d+(?:yr|yrs|year|years)$")
# A money amount in the original name (`1,5M`, `20k`); two names whose amounts
# differ are alternatives. 2026-10-01, HOS-178.
_AMOUNT = re.compile(r"(?<![0-9a-zа-яё])\d+(?:[.,]\d+)?\s?[mkмк](?![0-9a-zа-яё])")
_SOURCE_STEM = re.compile(r"[._-](?:src|source)$")
_DIGITS_LETTER = re.compile(r"^(\d+)[a-z]$")
_PAREN_NUMBER = re.compile(r"\(\d+\)")

_FULL_DATE = re.compile(r"(?<!\d)((?:19|20)\d{2})[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])(?!\d)")
_DMY_DATE = re.compile(r"(?<!\d)(0[1-9]|[12]\d|3[01])[-_.](0[1-9]|1[0-2])[-_.]((?:19|20)\d{2})(?!\d)")
_YEAR_MONTH = re.compile(r"(?<!\d)((?:19|20)\d{2})[-_.](0[1-9]|1[0-2])(?!\d)")
_YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_QUARTER = re.compile(r"(?<![0-9a-z])([qh][1-4])(?![0-9a-z])")
_MONTHS = {
    "january": "m01", "jan": "m01", "february": "m02", "feb": "m02", "march": "m03", "mar": "m03",
    "april": "m04", "apr": "m04", "may": "m05", "june": "m06", "jun": "m06", "july": "m07",
    "jul": "m07", "august": "m08", "aug": "m08", "september": "m09", "sep": "m09", "sept": "m09",
    "october": "m10", "oct": "m10", "november": "m11", "nov": "m11", "december": "m12", "dec": "m12",
}
_RU_MONTH = re.compile(
    r"^(январ|феврал|март|апрел|ма[йяе]$|июн|июл|август|сентябр|октябр|ноябр|декабр)")
_YEAR_OR_DAY = re.compile(r"^(?:(?:19|20)\d{2}|\d{1,2})$")
_COPY_MARKER = re.compile(r"copy|копия|\(\d+\)")
_RU_INDEX = {"январ": "m01", "феврал": "m02", "март": "m03", "апрел": "m04", "июн": "m06",
             "июл": "m07", "август": "m08", "сентябр": "m09", "октябр": "m10", "ноябр": "m11",
             "декабр": "m12"}


def _tokens(text: str) -> list[str]:
    """Name tokens of `text`: revision markers, dates and periods dropped."""
    cleaned = _PAREN_NUMBER.sub(" ", text.lower())
    for rx in (_FULL_DATE, _DMY_DATE, _YEAR_MONTH, _YEAR, _QUARTER):
        cleaned = rx.sub(" ", cleaned)
    tokens = []
    for tok in _TOKEN_SPLIT.split(cleaned):
        if not tok or _NOISE.match(tok):
            continue
        m = _DIGITS_LETTER.match(tok)
        tokens.append(m.group(1) if m else tok)
    return tokens


def normalised_name(rel: str) -> str:
    """The stem with revision markers and dates dropped; the stem itself if nothing is left."""
    stem = PurePosixPath(rel).stem.lower()
    return " ".join(_tokens(stem)) or stem


def _name_key(rel: str) -> str:
    """`normalised_name` with the separators gone: `ab cd` and `abcd` are one name."""
    return normalised_name(rel).replace(" ", "")


def folder_key(rel: str) -> str:
    """The parent directory's name tokens, sorted and joined; `""` when it has none."""
    return "".join(sorted(_tokens(PurePosixPath(rel).parent.name)))


def stamps(rel: str) -> frozenset[str]:
    """Every full day date the ORIGINAL file name carries: save stamps, not periods."""
    text = PurePosixPath(rel).stem.lower()
    found = {f"{m[1]}-{m[2]}-{m[3]}" for m in _FULL_DATE.finditer(text)}
    found |= {f"{m[3]}-{m[2]}-{m[1]}" for m in _DMY_DATE.finditer(_FULL_DATE.sub(" ", text))}
    return frozenset(found)


def periods(rel: str) -> frozenset[str]:
    """Every year, month or quarter the ORIGINAL file name carries; full dates are `stamps`."""
    text = PurePosixPath(rel).stem.lower()
    found: set[str] = set()
    text = _DMY_DATE.sub(" ", _FULL_DATE.sub(" ", text))
    for rx, fmt in ((_YEAR_MONTH, lambda m: f"{m[1]}-{m[2]}"),
                    (_YEAR, lambda m: m[1])):
        for m in rx.finditer(text):
            found.add(fmt(m))
        text = rx.sub(" ", text)
    for m in _QUARTER.finditer(text):
        found.add(m[1])
    toks = [t for t in _TOKEN_SPLIT.split(PurePosixPath(rel).stem.lower()) if t]
    for i, tok in enumerate(toks):
        # A month word is a period only beside a year or a day number:
        # "may-plan" and "marketing-march" name no period.
        near = toks[max(i - 1, 0):i] + toks[i + 1:i + 2]
        if not any(_YEAR_OR_DAY.match(t) for t in near):
            continue
        if tok in _MONTHS:
            found.add(_MONTHS[tok])
            continue
        ru = _RU_MONTH.match(tok)
        if ru:
            found.add(_RU_INDEX.get(ru[1], "m05"))
    return frozenset(found)


# ============================================================
# Text
# ============================================================

_WORD = re.compile(r"\w+")


def shingles(text: str) -> list[int]:
    """Sorted, unique 64-bit hashes of every 5-word run; a twin's provenance line dropped."""
    lines = [ln for ln in text.splitlines() if not ln.startswith("> Source SHA-256:")]
    words = _WORD.findall("\n".join(lines).lower())
    out = set()
    for i in range(len(words) - SHINGLE_WORDS + 1):
        piece = " ".join(words[i:i + SHINGLE_WORDS]).encode("utf-8")
        out.add(int.from_bytes(hashlib.blake2b(piece, digest_size=8).digest(), "big"))
    return sorted(out)


def default_cache_path() -> Path:
    """Beside the scan cache, under `runtime:hindsight-sync/cache`, resolved by logical name."""
    return data_path("runtime:hindsight-sync/cache") / "doc-detector-cache.json"


def load_cache(path: Path) -> dict[str, list]:
    """The shingle cache, or `{}` (reported on stderr) when absent or not a JSON object."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"doc_detector: cache at {path} is unreadable, starting empty: {exc}", file=sys.stderr)
        return {}
    if not isinstance(data, dict):
        print(f"doc_detector: cache at {path} is not an object, starting empty", file=sys.stderr)
        return {}
    return data


def save_cache(path: Path, cache: dict[str, list]) -> None:
    """Write the shingle cache atomically (same-directory tempfile, then replace)."""
    atomic_write_text(path, json.dumps(cache))


def _stamp(p: Path) -> list[int] | None:
    try:
        st = p.stat()
    except FileNotFoundError:
        return None
    return [st.st_size, st.st_mtime_ns]


def _text_entry(root: Path, rel: str, old: object, texts: dict | None) -> list | None:
    """`[size, mtime_ns, twin_stamp, payload]` for `rel`, or `None` if it changed while read."""
    real = root / rel
    before = _stamp(real)
    if before is None:
        return None
    is_md = PurePosixPath(rel).suffix.lower() == ".md"
    twin = None if is_md else doc_scope.twin_of(root, rel)
    twin_stamp = _stamp(twin) if twin is not None else None
    if (texts is None and isinstance(old, list) and len(old) == 4
            and old[0:3] == [before[0], before[1], twin_stamp]):
        return old
    if texts is not None and rel in texts:
        text = texts[rel]
        payload = "no usable text" if text is None else (shingles(text) or "empty extraction")
    elif not is_md and twin_stamp is None:
        payload = "no extract"
    elif (before[0] if is_md else twin_stamp[0]) > MAX_TEXT_BYTES:
        payload = f"text over {MAX_TEXT_BYTES} bytes"
    else:
        text = doc_scope.text_for(root, rel)
        payload = "no valid extract" if text is None else (shingles(text) or "empty extraction")
    if _stamp(real) != before:
        return None
    return [before[0], before[1], twin_stamp, payload]


# ============================================================
# Detection
# ============================================================


def _artifact_id(rel: str, sha: str) -> str:
    return hashlib.sha256(f"{rel}\n{sha}".encode()).hexdigest()[:32]


def _member(root: Path, rel: str, sha: str) -> dict | None:
    """The event's member record, or `None` when the file vanished since the scan."""
    try:
        st = (root / rel).stat()
    except FileNotFoundError:
        return None
    return {"artifact_id": _artifact_id(rel, sha), "path": rel, "sha256": sha,
            "size": st.st_size, "mtime": st.st_mtime}


def _family_shas(fam: vr.Family) -> set[str]:
    return {m.sha256 for m in list(fam.members.values()) + list(fam.proposed.values())}


def _dead(fam: vr.Family, state: vr.State) -> bool:
    """A family whose every revision sits inside one rejection: it holds nothing any more."""
    return vr.is_rejected(fam, state)


def _live_families(state: vr.State) -> list[vr.Family]:
    return [f for f in state.families.values() if not _dead(f, state)]


def _covered(shas: set[str], state: vr.State) -> str | None:
    """Why a proposal over `shas` must not be emitted, or `None`.

    Dead (rejected) families are not consulted: only the rejection itself
    governs their revisions, so a rejected family cannot freeze its files.
    """
    for fam in _live_families(state):
        existing = _family_shas(fam)
        if existing and (shas <= existing or existing <= shas):
            return f"overlaps family {fam.family_id}"
    for rej in state.rejections:
        if len(rej & shas) >= 2:
            return "holds two revisions of one rejected set"
    return None


def _names(a: str, b: str) -> tuple[bool, float | None, str]:
    """(names match, the text bar, or `None` when a name rule refuses, and why).

    Matching names face `SAME_NAME_THRESHOLD`. Names one of which holds all
    the other's tokens face `CONTAINED_NAME_THRESHOLD`, unless the extra
    tokens mark a source file or an alternative. Any other difference refuses.
    A period conflict raises either bar to `DATE_CONFLICT_THRESHOLD`.
    """
    pa, pb = periods(a), periods(b)
    floor = DATE_CONFLICT_THRESHOLD if (pa and pb and pa != pb) else 0.0
    if PurePosixPath(a).parent != PurePosixPath(b).parent and (
            folder_key(a) != folder_key(b)
            or len(_tokens(PurePosixPath(a).parent.name)) < MIN_FOLDER_TOKENS):
        return False, None, "different folders that do not name one document"
    if _name_key(a) == _name_key(b):
        return True, max(SAME_NAME_THRESHOLD, floor), ""
    ca, cb = Counter(_tokens(PurePosixPath(a).stem)), Counter(_tokens(PurePosixPath(b).stem))
    if not (ca <= cb or cb <= ca):
        return False, None, "names differ beyond one holding the other"
    extra = set((ca - cb) + (cb - ca))
    if extra & SOURCE_WORDS:
        return False, None, "source and build of one file"
    marks = sorted(extra & ALTERNATIVE_WORDS) + sorted(t for t in extra if _TERM.match(t))
    if marks:
        return False, None, f"alternative ({', '.join(marks)})"
    amounts = [set(_AMOUNT.findall(PurePosixPath(r).stem.lower())) for r in (a, b)]
    if amounts[0] != amounts[1]:
        return False, None, "alternative (different amounts)"
    return False, max(CONTAINED_NAME_THRESHOLD, floor), ""


def _pair_reason(a: str, b: str, score: float | None, why: str, threshold: float | None,
                 refusal: str) -> str:
    na, nb = normalised_name(a), normalised_name(b)
    name = f"name match ({na!r})" if _name_key(a) == _name_key(b) else f"name differs ({na!r} / {nb!r})"
    if refusal:
        name += f", refused: {refusal}"
    if score is None:
        text = f"comparison unavailable ({why})"
    elif threshold is None:
        text = f"text {score:.2f}"
    else:
        text = f"text {score:.2f} (threshold {threshold:.2f})"
    pa, pb = periods(a), periods(b)
    sa, sb = stamps(a), stamps(b)
    if pa and pb and pa != pb:
        date = f"date conflict ({', '.join(sorted(pa))} / {', '.join(sorted(pb))})"
    elif sa and sb and sa != sb:
        date = f"saved on different days ({', '.join(sorted(sa))} / {', '.join(sorted(sb))})"
    else:
        date = "no date conflict"
    return f"{a} ~ {b}: {name}; {text}; {date}"


def detect(state: vr.State, result: ScanResult, root: Path, *,
           texts: dict[str, str | None] | None = None,
           cache: dict[str, list] | None = None) -> Detection:
    """Everything this pass would propose, and what it saw but will not propose.

    `texts` (rel -> text or `None`) replaces `doc_scope.text_for` for the files
    it names; `cache` is the previous shingle cache. Neither is written to.
    """
    det = Detection()
    cache = cache or {}

    # Who already belongs to a live family (a rejected family holds nothing).
    member_paths: dict[str, list[tuple[vr.Family, vr.Member]]] = {}
    for fam in _live_families(state):
        for m in list(fam.members.values()) + list(fam.proposed.values()):
            member_paths.setdefault(m.path, []).append((fam, m))

    # Byte-identical copies: one representative each takes part, the rest are
    # reported. The representative is a path already in a family if there is
    # one, else one without a copy marker, else the first by name.
    dropped: set[str] = set()
    for _sha, paths in sorted(result.duplicates.items()):
        group = sorted(paths)
        det.duplicates.append(group)
        keep = min(group, key=lambda r: (r not in member_paths,
                                         bool(_COPY_MARKER.search(PurePosixPath(r).stem.lower())), r))
        dropped.update(r for r in group if r != keep)
    files = {rel: sha for rel, sha in result.files.items() if rel not in dropped}

    # Representations: one stem, one directory, several extensions; or a
    # built file beside its `.src` source.
    by_stem: dict[tuple[str, str], list[str]] = {}
    for rel in result.files:
        p = PurePosixPath(rel)
        by_stem.setdefault((str(p.parent), _SOURCE_STEM.sub("", p.stem.lower())), []).append(rel)
    for _key, rels in sorted(by_stem.items()):
        if len({PurePosixPath(r).name.lower() for r in rels}) > 1:
            det.representations.append(sorted(rels))

    # Possible moves: an absent member's bytes at a path no family knows.
    moved: set[str] = set()
    sha_paths: dict[str, list[str]] = {}
    for rel, sha in result.files.items():
        sha_paths.setdefault(sha, []).append(rel)
    for fam in _live_families(state):
        for m in list(fam.members.values()) + list(fam.proposed.values()):
            if m.state not in _STATELESS:
                continue
            for rel in sorted(sha_paths.get(m.sha256, [])):
                if rel in member_paths or rel in moved:
                    continue
                moved.add(rel)
                member = _member(root, rel, m.sha256)
                if member is None:
                    det.notes.append(f"vanished since the scan, not proposed: {rel}")
                    continue
                det.events.append({
                    "type": "member_proposed", "family_id": fam.family_id,
                    "source": "detector", "member": member,
                    "reasons": [f"possible move: {rel} carries the bytes of {m.path} ({m.state})"]})

    # Text, from the cache where the file has not changed.
    texted: dict[str, set[int]] = {}
    why_none: dict[str, str] = {}
    for rel in sorted(files):
        entry = _text_entry(root, rel, cache.get(rel), texts)
        if entry is None:
            det.notes.append(f"skipped until the next pass, changed while read: {rel}")
            continue
        det.cache[rel] = entry
        if isinstance(entry[3], list):
            texted[rel] = set(entry[3])
        else:
            why_none[rel] = str(entry[3])
    usable = [r for r in sorted(files) if r in texted or r in why_none]
    det.with_text, det.without_text = len(texted), len(why_none)
    df: Counter = Counter()
    for sh in texted.values():
        df.update(sh)
    n_docs = len(texted)
    weight = {sh: math.log(1 + n_docs / c) for sh, c in df.items()}
    total = {rel: sum(weight[x] for x in sh) for rel, sh in texted.items()}

    # Candidate pairs, blocked.
    pairs: set[tuple[str, str]] = set()
    name_blocks: dict[tuple, list[str]] = {}
    dir_blocks: dict[tuple, list[str]] = {}
    folder_blocks: dict[tuple, list[str]] = {}
    for rel in usable:
        p = PurePosixPath(rel)
        ext = p.suffix.lower()
        name_blocks.setdefault((_name_key(rel), ext, str(p.parent)), []).append(rel)
        if rel in texted:
            dir_blocks.setdefault((str(p.parent), ext), []).append(rel)
            if len(_tokens(p.parent.name)) >= MIN_FOLDER_TOKENS:
                folder_blocks.setdefault((folder_key(rel), _name_key(rel), ext), []).append(rel)
    for kind, blocks in (("name", name_blocks), ("directory", dir_blocks), ("folder", folder_blocks)):
        for key, rels in sorted(blocks.items()):
            if len(rels) < 2:
                continue
            if len(rels) > MAX_BLOCK_FILES:
                det.notes.append(f"{kind} block of {len(rels)} files not compared: {key[0]!r}")
                continue
            for i, a in enumerate(rels):
                for b in rels[i + 1:]:
                    pairs.add((a, b) if a < b else (b, a))

    # Score.
    links: dict[str, dict[str, str]] = {}
    for a, b in sorted(pairs):
        if files[a] == files[b]:
            continue
        if any({files[a], files[b]} <= rej for rej in state.rejections):
            continue  # a rejected pair is never an edge, under any name
        same, threshold, refusal = _names(a, b)
        if a in texted and b in texted:
            inter = sum(weight[x] for x in texted[a] & texted[b])
            union = total[a] + total[b] - inter
            score = inter / union if union else 0.0
            ok = threshold is not None and score >= threshold
            reason = _pair_reason(a, b, score, "", threshold, refusal)
            if not ok and score >= NEAR_MISS_FLOOR:
                det.near_misses.append((score, a, b, reason))
        else:
            why = why_none.get(a) or why_none.get(b) or "no text"
            # The name alone links only within ONE directory, never across a
            # period conflict or two different save stamps; sibling
            # directories need a text score.
            pa, pb, sa, sb = periods(a), periods(b), stamps(a), stamps(b)
            ok = (same and not (pa and pb and pa != pb) and not (sa and sb and sa != sb)
                  and PurePosixPath(a).parent == PurePosixPath(b).parent)
            reason = _pair_reason(a, b, None, why, threshold, refusal)
        if ok:
            links.setdefault(a, {})[b] = reason
            links.setdefault(b, {})[a] = reason
    det.near_misses.sort(key=lambda t: (-t[0], t[1], t[2]))

    # A new file linked into exactly one confirmed family: member_proposed.
    new_files = [r for r in sorted(links) if r not in member_paths and r not in moved]
    joined: set[str] = set()
    for rel in new_files:
        targets: dict[str, list[str]] = {}
        for other, reason in links[rel].items():
            for fam, _m in member_paths.get(other, []):
                if fam.confirmed:
                    targets.setdefault(fam.family_id, []).append(reason)
        if not targets:
            continue
        joined.add(rel)
        if len(targets) > 1:
            det.notes.append(f"linked to several confirmed families, not proposed: {rel}")
            continue
        fid, reasons = next(iter(targets.items()))
        fam = state.families[fid]
        shas = _family_shas(fam)
        if files[rel] in shas:
            continue
        if any(files[rel] in rej and rej & shas for rej in state.rejections):
            det.notes.append(f"would join family {fid} against a rejection, not proposed: {rel}")
            continue
        member = _member(root, rel, files[rel])
        if member is None:
            det.notes.append(f"vanished since the scan, not proposed: {rel}")
            continue
        det.events.append({"type": "member_proposed", "family_id": fid, "source": "detector",
                           "member": member, "reasons": sorted(reasons)})

    # The rest: connected groups of new files become proposed families.
    pool = [r for r in new_files if r not in joined]
    pool_set = set(pool)
    seen: set[str] = set()
    for start in pool:
        if start in seen:
            continue
        group, stack = [], [start]
        seen.add(start)
        while stack:
            cur = stack.pop()
            group.append(cur)
            for nxt in links.get(cur, {}):
                if nxt in pool_set and nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        if len(group) < 2:
            continue
        group.sort()
        shas = {files[r] for r in group}
        if len(shas) < 2:
            continue
        reasons = sorted({links[a][b] for a in group for b in links[a] if b in group and a < b})
        possible = len(group) * (len(group) - 1) // 2
        if len(reasons) / possible < MIN_GROUP_DENSITY:
            det.notes.append(f"group of {len(group)} not proposed, link density {len(reasons)}/{possible} "
                             f"under {MIN_GROUP_DENSITY}: {', '.join(group)}")
            continue
        refusal = _covered(shas, state)
        if refusal:
            det.notes.append(f"group of {len(group)} not proposed, {refusal}: {', '.join(group)}")
            continue
        members = [_member(root, r, files[r]) for r in group]
        if any(m is None for m in members):
            gone = [r for r, m in zip(group, members, strict=True) if m is None]
            det.notes.append(f"group not proposed, vanished since the scan: {', '.join(gone)}")
            continue
        det.events.append({"type": "family_proposed", "family_id": uuid.uuid4().hex, "source": "detector",
                           "members": members, "reasons": reasons})
    return det


def append_proposals(reg: Path, state: vr.State, det: Detection, result: ScanResult,
                     root: Path) -> list[dict]:
    """Append `det`'s proposals to `reg` as one batch; the caller holds `vr.locked(reg)`.

    When the registry moved since `state` was read, detection runs again on the
    fresh state (same scan, same cache). The whole batch is replayed on a trial
    first, so one bad event raises `RegistryCorrupt` with nothing written, never
    half a batch. Returns the events appended. Shared by `doc-versions.py scan`
    and `hindsight-sync.py push --detect` (2026-10-12).
    """
    existing = vr.read_events(reg)[0]
    fresh = vr.replay(existing)
    if fresh.generation != state.generation:
        det = detect(fresh, result, root, cache=det.cache)
    next_seq = (existing[-1]["seq"] + 1) if existing else 1
    trial = [{"schema_version": vr.SCHEMA_VERSION, "event_id": f"trial-{i}", "seq": next_seq + i,
              "ts": "", "actor": "helm", **ev} for i, ev in enumerate(det.events)]
    vr.replay(existing + trial)
    for ev in det.events:
        vr.append(ev, path=reg)
    return det.events
