#!/usr/bin/env python3
"""What makes two /census runs comparable, defined once.

The acceptance rule compares numbers produced days apart: the mechanical ceiling
measured on 2026-08-13, and the primitive's answers scored afterwards. That
comparison is only meaningful if both ran against the same world, so the values
in `PINNED_KEYS` are pinned and any divergence refuses the comparison rather
than reporting a difference that is really a difference of corpus.

This said "four values" twice while `PINNED_KEYS` has named five since
`corpus_content_sha256` was added (see the ORACLE_PINS comment below, which
records exactly that addition). A reader counting pins from the prose misread
what `states_comparable` refuses on by default, so the count now defers to the
tuple instead of restating it - a number written in two places is the one that
goes stale.

Why several and not one. The corpus SHA alone misses three real ways the world
moves underneath a run: `today` enters several oracles directly (a thread goes
stale by the calendar, not by an edit), the retrieved side depends on the index
config, and the index is rebuilt without the SHA moving at all.

This lives in `scripts/utils/` rather than inside `census-bench.py` because the
engine WRITES a state into its answers file and the scorer READS one, and a
comparison the whole acceptance rests on must not exist in two copies. The copy
that stops being updated is the one someone reads.
"""
from __future__ import annotations

import hashlib
import os
import subprocess  # nosec B404 - fixed argv git reads, never shell=True
from datetime import date, datetime
from pathlib import Path

from scripts.utils.excluded_dirs import NAMES as SHARED_EXCLUSIONS
from scripts.utils.excluded_dirs import prune as prune_excluded

PINNED_KEYS = ("corpus_sha", "corpus_content_sha256", "today",
               "index_config_sha256", "index_built")

# Not every pin governs every comparison, and treating them as one set makes a
# guard refuse comparisons it has no business refusing.
#
# ORACLE_PINS are the inputs the code-computed truth actually depends on: the
# corpus bytes, and the date several oracles read directly. Two answer sets
# graded against oracles are comparable exactly when these match.
#
# RETRIEVAL_PINS govern the retrieved side. They matter when comparing two
# RETRIEVAL measurements, and not at all when grading answers a traversal
# produced by reading files - /census never touches the index. Discovered on the
# first scored run, where a rebuilt index alone made a valid acceptance report
# NOT-COMPARABLE.
#
# `corpus_content_sha256` is in ORACLE_PINS and `corpus_sha` alone is not enough:
# the commit is not the bytes, and on a live operator workspace the tree is
# dirty nearly always. See `corpus_digest`.
ORACLE_PINS = ("corpus_sha", "corpus_content_sha256", "today")
RETRIEVAL_PINS = ("index_config_sha256", "index_built")


def git_head(path: Path) -> tuple[str, bool]:
    """(HEAD sha, working tree dirty). Unknown-and-dirty on any failure.

    Fail-toward-dirty is deliberate: an unreadable repository must not report a
    clean, comparable state it cannot establish.
    """
    try:
        head = subprocess.run(  # nosec B603 B607 - fixed argv, no shell
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        dirty = subprocess.run(  # nosec B603 B607 - fixed argv, no shell
            ["git", "-C", str(path), "status", "--porcelain"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown", True
    # Check the EXIT CODES, not just stdout. `except OSError` alone only covers a
    # missing git binary. When git ran and refused -- not a repository, a broken
    # .git, a repo with no commit yet -- stdout was empty or, for `rev-parse
    # HEAD` on an empty repo, the literal string "HEAD"; `bool("".strip())` then
    # read as CLEAN and the function returned a state it could not establish as
    # comparable. That is the exact opposite of the fail-toward-dirty promise
    # three lines above.
    if head.returncode != 0 or dirty.returncode != 0:
        return "unknown", True
    return head.stdout.strip() or "unknown", bool(dirty.stdout.strip())


# Files the corpus digest covers. The same set the engine's window check walks,
# so "the corpus" means one thing in both places.
CORPUS_SUFFIXES = (".md", ".json", ".yaml", ".yml", ".txt")

# Directories excluded from the digest: rebuilt artefacts, not corpus content.
# The holding pens come from `scripts/utils/excluded_dirs.py` (HOS-28); the
# runtime store is skipped by its registry name in `corpus_digest`.
DIGEST_SKIP_DIRS = frozenset({".git", "node_modules", "__pycache__"} | SHARED_EXCLUSIONS)

def digest_scopes() -> tuple[str, ...]:
    """The subtrees whose bytes can move a truth, each by its registry name.

    The directories the oracles read, deliberately NOT the whole data overlay:
    a benchmark that digests the place it writes its reports moves its own pin
    (measured 2026-08-13). Names, not literals, so a workspace that lays its
    data out differently digests ITS directories. Relative to the data root; an
    absent directory hashes as `<absent>`, stably. A name already listed is not
    listed twice; a scope outside the root is named `<unreachable:...>` rather
    than dropped.
    """
    from scripts.utils.data_path import data_path
    from scripts.utils.threads_lib import business_thread_dirs
    from scripts.utils.workspace import get_auto_memory_dir, get_data_root

    root = get_data_root().resolve()
    scopes = (
        data_path("threads-business"),
        data_path("crm-contacts"),
        data_path("context-entity"),
        data_path("context-matter"),   # the `pipeline` file
        # The `people` and `friends` files by their own names: the C3 move
        # takes both out of `context-entity` (HOS-234 part 8).
        data_path("people").parent,
        data_path("friends").parent,
        get_auto_memory_dir(),                     # stays; no seam
        # Each account's threads (HOS-234 part 10), which the C3 move takes out
        # of `threads-business`; an address already listed is not listed twice.
        *business_thread_dirs(),
    )
    names: list[str] = []
    for directory in scopes:
        try:
            name = directory.resolve().relative_to(root).as_posix()
        except (ValueError, OSError):
            # A scope outside the corpus root cannot be addressed relative to it.
            # Skipping it silently would shrink the digest's coverage without
            # saying so, so it is named instead and hashed as unreachable.
            name = f"<unreachable:{directory.name}>"
        if name not in names:
            names.append(name)
    return tuple(names)


def corpus_digest(corpus_root: Path, scopes: tuple[str, ...] | None = None) -> str:
    """A content hash of the corpus, for when git HEAD cannot answer the question.

    `git_head` establishes WHICH COMMIT, and the acceptance needs WHICH BYTES.
    They part company the moment the tree is dirty, which for a live operator
    workspace is almost always: on 2026-08-13 a `/thread log` written between two
    scoring runs moved `oracle_agg_09`'s truth from 8 threads to 9 -- the log text
    named a country -- while `corpus_sha` stayed byte-identical and
    `states_comparable` reported True. A correct answer was graded wrong and the
    guard whose whole job is that comparison said nothing.

    Costs one walk of ~1.4 MB. Paths are relative and sorted, so the digest is
    stable across machines and across the order the filesystem hands files back.

    The exclusions PRUNE, and until 2026-09-21 they filtered. `rglob("*")`
    listed every path under a scope and `DIGEST_SKIP_DIRS` then dropped the
    excluded ones, so an exclusion saved a hash update and paid the whole
    descent into the tree it had already decided to ignore. That is cheap only
    while every scope sits in a named subdirectory, which is today's layout and
    not a property of this function: raise a scope to the overlay root and it
    pays the whole tree, silently. The same shape cost 9.55 seconds a walk in
    `scripts/utils/overlay_write_guard.py` against its own written estimate of
    about 50 ms, MEASURED 2026-09-20, and nothing reported the 190-fold gap.

    The RESULT is unchanged, deliberately, and the two things that could have
    moved it are handled rather than assumed. The sort is taken over the files
    alone instead of over files and directories interleaved, which leaves the
    files in the same relative order. And the old filter intersected the
    excluded names with EVERY part of the path including the filename, while a
    prune can only match a directory; no excluded name can reach that branch,
    because each of them is either dotted or hyphenated with no dot, so
    `Path(name).suffix` is the empty string and `CORPUS_SUFFIXES` has already
    refused it one line above.
    """
    digest = hashlib.sha256()
    root = corpus_root.resolve()
    from scripts.utils.runtime_dir import store_dirname
    store = store_dirname(root)
    scopes = digest_scopes() if scopes is None else scopes
    # A scope that does not exist is hashed as absent rather than skipped: the
    # difference between "empty" and "gone" is a difference of corpus.
    for scope in scopes:
        base = root / scope
        digest.update(f"[{scope}]".encode())
        if not base.is_dir():
            digest.update(b"<absent>\0")
            continue
        # A scope whose own address carries an excluded name contributed no
        # file under the old filter either, because the intersection was taken
        # against the path relative to the ROOT and so covered the scope
        # prefix. It keeps its marker and is not walked.
        scope_parts = base.relative_to(root).parts
        scope_rel = "/".join(scope_parts)
        if (DIGEST_SKIP_DIRS.intersection(scope_parts) or scope_rel == store
                or scope_rel.startswith(f"{store}/")):
            continue
        found = []
        for dirpath, dirnames, filenames in os.walk(base):
            prune_excluded(dirnames, DIGEST_SKIP_DIRS)
            here = Path(dirpath)
            # The runtime store by the tree's registry name, at its own
            # root-relative address (nested since HOS-241): a relocated store's
            # name is no reason to skip a directory of that name elsewhere.
            here_rel = here.relative_to(root).as_posix()
            dirnames[:] = [d for d in dirnames
                           if (d if here_rel == "." else f"{here_rel}/{d}") != store]
            found.extend(here / name for name in filenames
                         if Path(name).suffix.lower() in CORPUS_SUFFIXES)
        for path in sorted(found):
            if not path.is_file():
                continue
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(b"\0")
            try:
                digest.update(path.read_bytes())
            except OSError:
                # A file the walk saw but cannot read changes the digest rather
                # than being skipped: silence here would report two different
                # corpora as the same one, which is what this closes.
                digest.update(b"<unreadable>")
            digest.update(b"\0")
    return digest.hexdigest()[:16]


def run_state(corpus_root: Path, engine_root: Path, today: date, tz=None) -> dict:
    """The pinned values, plus the dirty flag for the reader's judgement."""
    sha, dirty = git_head(corpus_root)
    config_path = engine_root / "config" / "memory-index.yaml"
    config_hash = (
        hashlib.sha256(config_path.read_bytes()).hexdigest()[:16]
        if config_path.exists() else "absent"
    )
    from scripts.utils.data_path import data_path
    index_db = data_path("runtime:memory-index/cache", data_root=corpus_root) / "index.db"
    return {
        "corpus_sha": sha,
        "corpus_dirty": dirty,
        "corpus_content_sha256": corpus_digest(corpus_root),
        "today": today.isoformat(),
        "index_config_sha256": config_hash,
        "index_built": (
            datetime.fromtimestamp(index_db.stat().st_mtime, tz=tz).isoformat()
            if index_db.exists() else "absent"
        ),
    }


def states_comparable(a: dict, b: dict,
                      pins: tuple[str, ...] = PINNED_KEYS) -> tuple[bool, list[str]]:
    """Which of the given pinned values diverge between two runs."""
    diverged = [k for k in pins if a.get(k) != b.get(k)]
    return (not diverged), diverged
