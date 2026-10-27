"""One JSONL log primitive for append-only logs.

``append_jsonl`` is a real ``O_APPEND`` write of one line. POSIX makes the
seek-to-end and the write a single operation for a file opened that way, so a
second process cannot land inside the first one's line and cannot overwrite it.
The read-modify-rewrite it replaced lost updates between processes even under
``atomic_write_text`` and a ``threading.Lock`` (2026-08-23 engine audit,
finding 15).

``read_jsonl_capped`` is the read half. Over the size cap it keeps the TAIL,
the most recent entries, and reports that it truncated, rather than returning
an empty list while the writers keep appending (finding 14).
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def append_jsonl(path: Path, entry: dict, mode: int = 0o644) -> None:
    """Append one JSON object as a line. Raises OSError on failure.

    Opened ``O_APPEND``, so the offset lookup and the write are one atomic
    operation and a concurrent writer in another PROCESS cannot lose the entry
    (a ``threading.Lock`` only covers the current one).

    A file whose last byte is not a newline -- a hand-edit, or a crash mid-line
    under the old rewrite path -- gets one inserted first, so the repaired line
    stays parseable rather than being glued to this entry.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry) + "\n"
    existed = path.exists()
    needs_newline = False
    if existed:
        try:
            size = path.stat().st_size
            if size:
                with path.open("rb") as f:
                    f.seek(-1, os.SEEK_END)
                    needs_newline = f.read(1) != b"\n"
        except OSError:
            # Unreadable tail: append anyway rather than refuse the write. A
            # stray glued line is recoverable; a dropped record is not.
            logger.warning("could not inspect the tail of %s", path, exc_info=True)
    precreated = False
    if not existed:
        # Create it EMPTY, at the requested mode, before a byte goes in.
        # `open("a")` creates at the process umask -- commonly 0o644 -- and the
        # chmod below landed only AFTER the first record was written, so a
        # caller asking for 0o600 got a window in which the file was
        # world-readable and already held content. `scripts/utils/atomic.py`
        # carries a comment about closing exactly this race by
        # chmodding before `os.replace`; this function had reopened it.
        #
        # The chmod after stays. `os.open` masks the mode with the umask, so
        # under a strict umask a requested 0o644 would come out 0o600; the
        # chmod restores the caller's intent. That direction is safe -- briefly
        # too NARROW, never too wide.
        try:
            os.close(os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, mode))
            precreated = True
        except OSError:
            logger.warning("could not pre-create %s at mode %o", path, mode,
                           exc_info=True)
    with path.open("a", encoding="utf-8") as f:
        if not existed and not precreated:
            # The paragraph above said "before a byte goes in" over a body with
            # a fallback in it, and this branch IS the fallback: when the
            # pre-create raised, the `open("a")` a line up is what created the
            # file, at the process umask, and the chmod at the bottom lands
            # AFTER the record. That is the same window, reopened by the error
            # path of the fix that closed it. These logs carry subject lines,
            # so on a multi-user host it is a real disclosure. Narrow the descriptor we
            # already hold, before any content: `fchmod` takes the open fd, so
            # it cannot be redirected by a symlink swapped in underneath the
            # path. Retired 2026-09-02.
            try:
                os.fchmod(f.fileno(), mode)
            except (OSError, AttributeError):  # pragma: no cover - Windows has no fchmod
                logger.warning("could not narrow %s to mode %o before writing",
                               path, mode, exc_info=True)
        if needs_newline:
            f.write("\n")
        f.write(line)
    if not existed:
        try:
            os.chmod(path, mode)
        except OSError:  # pragma: no cover - Windows chmod is a partial no-op
            logger.warning("could not set mode %o on %s", mode, path, exc_info=True)


def read_jsonl_capped(path: Path, max_bytes: int) -> tuple[list[dict], bool]:
    """Return ``(entries, truncated)`` for an append-only JSONL log.

    Reads at most ``max_bytes`` from the END of the file. Over the cap the
    leading partial line is dropped and ``truncated`` is True -- the caller
    shows the recent entries instead of the old ``return []``, which showed
    nothing and said nothing.

    **Truncation is logged here, by this function**, because callers drop the
    flag; on a dedupe log a dropped head lets an old entry be acted on again.

    A missing file is ``([], False)``: genuinely empty, not truncated.
    Unparseable lines are skipped; non-dict JSON values are skipped.
    """
    if not path.exists():
        return [], False
    try:
        size = path.stat().st_size
        truncated = size > max_bytes
        with path.open("rb") as f:
            if truncated:
                f.seek(size - max_bytes)
            blob = f.read()
    except OSError:
        logger.warning("could not read %s", path, exc_info=True)
        return [], False
    text = blob.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if truncated and lines:
        # The first line almost certainly starts mid-record.
        lines = lines[1:]
        logger.warning(
            "%s is %d bytes, over the %d cap: read the newest %d only. Entries "
            "older than that are NOT in the returned set, so anything recorded "
            "there reads as never-actioned. Compact or rotate this log.",
            path, size, max_bytes, max_bytes,
        )
    out: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out, truncated
