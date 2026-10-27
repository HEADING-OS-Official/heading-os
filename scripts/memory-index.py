#!/usr/bin/env python3
"""Local associative-memory index -- build / query / stats.

Semantic recall across the workspace's ceo-only business memory (Odin brain,
business threads, business CRM), computed entirely locally with bge-m3 on the
model server this machine declares (Ollama or Lemonade Server; see
`embeddings.declared_embed_provider`).
Finds notes by MEANING, cross-lingual (RU <-> EN), where grep finds only exact
words. The score itself is a salience signal (hits ~0.56-0.61, empty ~0.49-0.53
on the tested Odin corpus; default threshold 0.55).

Usage:
    python3 scripts/memory-index.py build [--force]
    python3 scripts/memory-index.py refresh      # the hourly job: companions, then build
    python3 scripts/memory-index.py query "<text>" [--layer L] [--top-k N] [--threshold T]
    python3 scripts/memory-index.py stats

Recall is HYBRID: a dense channel (bge-m3 cosine, gated by the threshold) and a
sparse channel (SQLite FTS5 BM25 lexical match), fused by Reciprocal Rank Fusion.
The dense gate is untouched, so the honest-gap signal survives: a query reports
"nothing" only when BOTH channels are empty. BM25 rescues the known near-miss
where the material is distributed across notes with no single dedicated one for
a dense vector to crystallise above threshold; a BM25-only hit must also clear a
semantic-adjacency gate, so a generic-term match cannot leak through as noise.
FTS5 + bm25() ship with the stdlib sqlite3 -- no new dependency.

The index ("hippocampus") lives at the registry name
`runtime:memory-index/cache`, file `index.db` (`content_store_path`):
gitignored, a rebuildable cache, never source of truth. The layers ("neocortex") stay in git.

Denial is structural: a denied path is NEVER read, so it can never be
retrieved. What is denied comes from `config/memory-index.yaml` and from
nowhere else, since 2026-09-12. `scripts/utils/air_gap.py` used to add an
unconditional `_secure/` prefix and `personal` segment on top of the config;
the operator removed both, on the ground that this machine has one user and
nothing on it is withheld from his own index. `CEO-Only` survives meaning "not
for the other executives", and that marking is enforced on the paths that
publish to one (`scripts/utils/sensitivity.py`), which is where it still can be.
The config's remaining entry is `deny_prefixes: ["_secure/"]`, over a vault
directory that no longer exists.

This is a ceo-only tool, and the whole of the operator's own corpus is what it
is meant to recall: business CRM, personal chronicle, the personal datastore
subtree, all of it.
"""

import argparse
import json
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from itertools import groupby
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import numpy as np
    import yaml
except ImportError as exc:
    sys.stderr.write(
        f"Missing runtime dependency: {exc.name}. "
        f"Install with: uv sync\n"
    )
    sys.exit(1)

from scripts.utils import salience
from scripts.utils.air_gap import is_denied
from scripts.utils.clone_guard import require_main_clone
from scripts.utils.venv_guard import venv_python
from scripts.utils.colors import BOLD, CYAN, GRAY, GREEN, RED, RESET, YELLOW
from scripts.utils.commit_source import iter_commits
from scripts.utils.excluded_dirs import NAMES as SHARED_EXCLUSIONS
from scripts.utils.excluded_dirs import WORK_DIRNAME
from scripts.utils.symbol_source import iter_symbols
from scripts.utils.embeddings import (
    EmbeddingError,
    embed,
    declared_embed_provider,
    embed_auth_headers,
    embed_host_remedy,
    index_embed_preference,
    index_embed_provider,
    model_digest,
)
from scripts.utils.repo_files import read_sources
from scripts.utils.data_path import DataPathError, data_path, data_write_dir
from scripts.utils.memory_layers import resolve_layers
from scripts.utils.runtime_dir import default_store_dirname, store_dirname
from scripts.utils.inference_host import (
    EMBED_HOST_ENV,
    LOCAL_HOST,
    InferenceHostUnavailable,
    resolve_inference_host,
    resolve_pinned_host as _resolve_embed_host,
)
from scripts.utils.workspace import (
    RoutingRefused,
    get_classification,
    get_data_root,
    get_workspace_root,
    load_env,
)

# ============================================================
# Configuration
# ============================================================

CONFIG_REL = "config/memory-index.yaml"
STORE_NAME = "index.db"
# The content store, RELATIVE TO THE DATA ROOT, is `content_store_rel(root)`:
# what `content_store_path(root)` resolves to, as the relative string the lock
# and the move ledger want. It was the constant `STORE_REL` until HOS-5 F1
# (2026-09-25), when the store's directory became the registry's
# `runtime_store:` key; a constant cannot say which tree's registry it read.
def content_store_rel(root: Path) -> str:
    """`<store>/memory-index/cache/index.db`, `<store>` read from `root`'s registry."""
    return f"{store_dirname(root)}/memory-index/cache/{STORE_NAME}"


# Code-collection store. Content layers index the CEO's private DATA tree into
# `content_store_path()` (the runtime-junk store under the data root); the
# `code` collection (skill/rule) indexes the ENGINE tree into this store inside
# the engine clone. Each artifact on its own side of the engine/data seam: no
# cross-seam join, engine self-sufficient, no public-code rows in the private
# data repo. Gitignored, rebuildable cache. The code store is NOT runtime junk
# of the overlay, so it keeps its own dotted directory in the engine tree.
CODE_STORE_REL = ".memory-index-code/index.db"


def content_store_path(data_root: Path, *, create: bool = False) -> Path:
    """`index.db` under the registry name `runtime:memory-index/cache`.

    The root is passed in rather than resolved here, so `get_data_root` on this
    module keeps deciding where the store goes. `create=True` is the BUILD's
    call and goes through `data_write_dir`; a read path (`query`, `stats`, the
    touch mirror) resolves only, because a read that materialises the directory
    is a defect, and nothing guards against adding `create=True` to one.
    `content_store_rel` above spells the same address as a relative string for
    the store lock.
    """
    root = Path(data_root)
    if create:
        return data_write_dir("runtime:memory-index/cache", data_root=root) / STORE_NAME
    return data_path("runtime:memory-index/cache", data_root=root) / STORE_NAME
SNIPPET_CHARS = 500  # body snippet length for embedding; also query truncation
# Ceiling on ONE embed text, used when `chunk.embed_max_chars` is absent from
# config. The number and the measurements behind it are argued in
# `config/memory-index.yaml` beside that key; the short form is that the embed
# server refuses a single input past bge-m3's 8192-TOKEN context, and the
# position of that wall in characters ranged from 40,000 down to 8,037 across
# the content shapes measured on 2026-09-24.
EMBED_MAX_CHARS_DEFAULT = 6000

# Hybrid recall: sparse (BM25) channel + Reciprocal Rank Fusion.
RRF_K = 60            # RRF damping constant (standard); higher = flatter rank weighting
SPARSE_LIMIT = 40     # max BM25 candidates pulled from FTS before fusion
RANK_CAP = 50         # max dense candidates carried into fusion
# Convergence gate: a BM25-only hit surfaces only if it is ALSO semantically
# adjacent (cosine within this margin below the dense threshold). Drops the
# fan-out noise where a generic term ("value", "strategy") lexically matches
# unrelated notes; keeps genuine near-misses sitting just below threshold.
SPARSE_COS_MARGIN = 0.05
# Path-match channel: a query token that matches a file's PATH (folder/project/
# client name) is a high-precision identifier and is admitted BYPASSING the
# convergence gate -- but only when the token is RARE (document-frequency at or
# below this cap). A token matching more files than this is a generic directory
# word (outputs, datastore, a year) and stays gated, so it cannot flood results.
PATH_TOKEN_DF_CAP = 25
# Long enough to outlast a per-file build commit, short enough that a
# genuinely wedged store still fails rather than hanging a recall.
SQLITE_BUSY_TIMEOUT_S = 30.0
TOKEN_RE = re.compile(r"\w+", re.UNICODE)  # query -> bare word tokens (RU/EN)

# The deny predicate lives in scripts/utils/air_gap.py, imported above. It
# applies the two lists passed to it and nothing else; the unconditional denies
# it used to carry were removed on 2026-09-12. Do not re-inline a copy here, and
# do not re-add a floor inside it: a caller that wants a deny states it.


def _fold_in_shared_denies(cfg: dict) -> None:
    """Add the operator's holding pens to `cfg["deny_prefixes"]`, in place.

    They used to be written into `config/memory-index.yaml`, which made that
    file the fifth independent copy of one name (HOS-28). They are root-anchored
    prefixes, the same shape the file's own entries already have, so nothing
    downstream can tell which of them came from where. Order is preserved and
    duplicates dropped, so a config that still spells one out keeps working and
    gains nothing by it.
    """
    cfg["deny_prefixes"] = list(dict.fromkeys(
        list(cfg["deny_prefixes"])
        + [f"{name}/" for name in sorted(SHARED_EXCLUSIONS)]))


def file_deny_segments(deny_segments) -> tuple:
    """`deny_segments` plus `_work`, for the walk over FILES and nothing else.

    HOS-195: the working folder beside a document is not memory, so no file
    inside one is indexed, at any depth. It is not folded into
    `cfg["deny_segments"]`, because `iter_commits` denies a WHOLE commit when
    any path it touched matches, which is right for a secret and wrong here: the
    commit that moves a document's helpers into `_work/` would vanish from the
    history layer with its message. The single spelling is `WORK_DIRNAME` in
    `scripts/utils/excluded_dirs.py`.
    """
    return tuple(dict.fromkeys([*deny_segments, WORK_DIRNAME]))


def store_denies(deny_prefixes, root: Path, tree) -> list:
    """`deny_prefixes` plus the runtime store under its REGISTRY name, when `root` is the data tree.

    `config/memory-index.yaml` denies the store by its default name, and it is
    read from the ENGINE root, which cannot know what a data tree's registry
    calls its store. A registry that moves it (HOS-5 F1, 2026-09-25) would
    otherwise have the whole store indexed as content: calendar dumps, queues,
    cookies. So the deny is added where the data root is in hand.
    """
    if tree != "data":
        return list(deny_prefixes)
    return list(dict.fromkeys(list(deny_prefixes) + [f"{store_dirname(Path(root))}/"]))


def runtime_store_prefixes(root: Path | None = None) -> tuple:
    """The deny prefixes that name the runtime store: its default name, and `root`'s registry name.

    `config/memory-index.yaml` denies the store by the name the ENGINE's default
    registry gives it, and `store_denies` adds the name `root`'s own registry
    gives it, so lifting the store for a layer has to lift both. Both are read
    from the registry, never spelled here (HOS-5 section 6, HOS-206).
    """
    names = [default_store_dirname()]
    if root is not None:
        names.append(store_dirname(Path(root)))
    return tuple(dict.fromkeys(f"{name}/" for name in names))


def layer_deny_prefixes(layer_cfg: dict, deny_prefixes, store_prefixes=None) -> list:
    """The prefixes denied to ONE layer: all of them, unless the layer says `under_runtime_store: true`.

    Such a layer, and only it, is let under the runtime store: every prefix in
    `store_prefixes` (by default `runtime_store_prefixes()`) is lifted for it,
    and every other deny still applies. HOS-203
    (2026-10-05): the Hindsight snapshot lives under the store so that
    `hindsight-sync.py` can never send it back into the bank it copies, and the
    index must still reach it to answer when Hindsight does not. Its glob names the
    one service directory, so the calendar dumps, queues and cookies beside it stay
    out exactly as before.
    """
    if not layer_cfg.get("under_runtime_store"):
        return list(deny_prefixes)
    if store_prefixes is None:
        store_prefixes = runtime_store_prefixes()
    return [p for p in deny_prefixes if p not in set(store_prefixes)]


def config_auth_headers(cfg: dict) -> dict:
    """The embed host's credential, resolved from the root THIS cfg was loaded from.

    The one way anything downstream of `load_config` asks for it, and it takes
    no root argument on purpose. Every caller that picks its own root is a
    caller that can pick the wrong one, which is precisely what happened:
    `_build_store` passed the root of the STORE it was writing, so the content
    store -- whose root is the data overlay, holding neither config file --
    resolved an empty key name and sent an unauthenticated request to a
    key-gated server. MEASURED 2026-09-24 in HELM: `HTTP 401` from the embed
    endpoint and `Committed 0 files (0 / 30114 chunks)`, hourly, 34 times.

    Same shape as `ProbeTarget` in `scripts/utils/inference_host.py` and for the
    same reason (HOS-101, 2026-09-24): the fix is not N callers each
    remembering to resolve correctly, it is removing the choice from them.

    The value is returned and never stored in `cfg`. That dict is printed by
    `stats`, carried into run records and handed to every subcommand, and a key
    that lives in it will eventually be written to a file by something that
    never thought about keys.

    Raises:
        ValueError: the config NAMES a key variable and nothing has set it.
    """
    return embed_auth_headers(root=Path(cfg["config_root"]))


def load_config(root: Path, *, allow_fallback: bool = False) -> dict:
    """Load config/memory-index.yaml. Clear error if absent or unparseable.

    `allow_fallback` carries `build --allow-host-fallback` down to host
    resolution: it is the one documented way to embed somewhere other than the
    pinned host.
    """
    path = root / CONFIG_REL
    if not path.exists():
        sys.stderr.write(f"Config not found: {path}\n")
        sys.exit(1)
    # `UnicodeDecodeError` beside `yaml.YAMLError`: the decode happens in
    # `read_text`, before PyYAML is handed anything, and a decode error is a
    # `ValueError` that no YAML clause catches. MEASURED 2026-09-01 against a
    # `config/memory-index.yaml` holding one 0xff byte - a raw traceback where
    # the two lines above promise a named message and exit 1.
    try:
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (UnicodeDecodeError, yaml.YAMLError) as e:
        sys.stderr.write(f"Cannot parse {path}: {e}\n")
        sys.exit(1)
    cfg.setdefault("model", "bge-m3")
    # WHICH ROOT THIS CONFIG CAME FROM, carried with it so that nothing
    # downstream has to choose one again.
    #
    # Every embed fact this function resolves -- host, provider, and the NAME of
    # the credential variable -- is read from `root` and from the machine-local
    # file beside it. A later caller that re-resolves any of them has to supply
    # a root of its own, and `_build_store` supplied the wrong one for six
    # months' worth of a different question: the STORE's root. MEASURED
    # 2026-09-24 on this branch, the two answers for the content store:
    #
    #     root=<engine>  api_key_env 'LEMONADE_API_KEY'  provider 'lemonade'
    #     root=<data>    api_key_env ''                  provider 'ollama'
    #
    # The data root holds neither `config/memory-index.yaml` nor
    # `config/inference-hosts.yaml`, and it is not a linked worktree of the engine,
    # so `_machine_payload`'s main-checkout fallback does not reach it either.
    # Both lookups therefore answered "this host needs no key" and the build
    # embedded unauthenticated.
    #
    # A path is not a secret, so it lives in `cfg` where the resolved headers
    # deliberately do not (see below). `config_auth_headers` is the only way
    # anything downstream of here asks for the credential, so a caller cannot
    # hold the host without holding the key that host requires.
    cfg["config_root"] = str(root)
    # Resolved, not merely defaulted. `host` may read `auto:<port>`, which
    # follows the CURRENT WSL gateway - a literal gateway address would work
    # until the next WSL restart and then break every scheduled refresh
    # silently. HEADING_OS_INFERENCE_EMBED_HOST overrides for a one-off run
    # without editing config.
    #
    # A configured host is a PIN (operator directive, 2026-08-23): when nothing
    # it names answers, `cfg["host"]` is left None and `cfg["host_error"]`
    # carries why. Resolution does NOT abort here, because `stats` and `meta`
    # never embed and must keep working while the embedder is down; the two
    # commands that DO embed refuse on the None. `allow_fallback` is the named
    # override and restores the old degrade-to-local behaviour.
    # The preference comes from `index_embed_preference()`, not from this file
    # alone: since 2026-08-23 the machine's own pin lives in the gitignored
    # `config/inference-hosts.yaml`, because an address is a fact about one computer
    # and this config is tracked. Reading only `cfg["host"]` here would resolve a
    # pinned machine to the local daemon - which on this laptop no longer has the
    # embed model at all.
    cfg["host_preferred"] = index_embed_preference(root=root)
    cfg["host"] = cfg["host_preferred"]
    cfg["host_error"] = None
    # Which PROGRAM answers there. Stamped into the store beside the model and
    # the digest, so a row can say which of two producers wrote it.
    cfg["provider"] = index_embed_provider(root=root)
    # What to tell a person when that host does not answer: the DECLARED
    # program, or "unknown", never the store's "ollama" default (HOS-215).
    # Plain text, no colour, because `query --json` carries it to the recall
    # hook. The missing-key branch below replaces it with its own remedy.
    cfg["host_remedy"] = embed_host_remedy(
        provider=declared_embed_provider(root=root), host=cfg["host_preferred"])

    # The credential that host requires, if it declares one. Resolved into a
    # LOCAL name and deliberately never into `cfg`: this dict is printed by
    # `stats`, carried into run records and handed to every subcommand, and a
    # key that lives in it will eventually be written to a file by something
    # that never thought about keys.
    try:
        embed_headers = config_auth_headers(cfg)
    except ValueError as exc:
        # A key NAMED in config and absent from the environment. The
        # consequence is identical to a dead host -- nothing can be embedded --
        # so it takes the same path and is announced the same way. The message
        # `load_api_key` builds names the VARIABLE and never its value.
        embed_headers = None
        cfg["host"] = None
        cfg["host_error"] = str(exc)
        cfg["host_remedy"] = (
            "The embed host is reachable or not; nothing asked it. A key is "
            "NAMED in config and is not set. Set that variable in .env, or "
            "clear `api_key_env` (config/memory-index.yaml) / "
            "`embed_api_key_env` (config/inference-hosts.yaml) if the host "
            "needs no key."
        )
        sys.stderr.write(embedder_down_banner(
            str(exc),
            remedy=(
                f"{YELLOW}  The embed host is reachable or not; nothing asked "
                f"it. A key is NAMED in config and is not set.{RESET}\n"
                f"{YELLOW}  Set that variable in .env, or clear `api_key_env` "
                f"(config/memory-index.yaml) / `embed_api_key_env` "
                f"(config/inference-hosts.yaml) if the host needs no key.{RESET}\n"
            ),
        ))

    if embed_headers is None:
        # Nothing to resolve a host FOR. Probing without the credential the host
        # requires would report a live server as dead and send the reader to the
        # embedder instead of to the one missing variable.
        pass
    elif allow_fallback:
        cfg["host"] = resolve_inference_host(
            cfg.get("host"), env_var=EMBED_HOST_ENV,
            headers=embed_headers,
        )
        # Every subcommand goes through here, so the announcement cannot be
        # forgotten by a code path added later. Operator directive, 2026-08-21:
        # a fallback is said out loud, at once, in red.
        banner = host_fallback_banner(cfg["host_preferred"], cfg["host"],
                                      remedy=cfg["host_remedy"])
        if banner:
            sys.stderr.write(banner)
    else:
        try:
            cfg["host"] = _resolve_embed_host(cfg.get("host"), headers=embed_headers)
        except InferenceHostUnavailable as exc:
            cfg["host"] = None
            cfg["host_error"] = str(exc)
            sys.stderr.write(embedder_down_banner(
                str(exc), remedy=_remedy_lines(cfg["host_remedy"])))
    cfg.setdefault("threshold", 0.55)
    # The calibrated cut, if a calibration run has recorded one. Kept under its
    # OWN key so `cfg["threshold"]` remains the value this repository ships,
    # which is what a test asserted against the shipped file until the
    # 2026-09-21 test-suite split. The separation is still the right shape; it
    # is simply no longer checked.
    cfg["calibrated_threshold"] = calibrated_threshold()
    cfg.setdefault("near_miss_margin", 0.12)
    cfg.setdefault("top_k", 8)
    cfg.setdefault("layers", [])
    # A layer that NAMES its directories (`names:`, `files:`) is turned into a
    # data-root-relative `glob` here, from the registry, so a repointed name
    # is followed with no edit to this file. `scripts/utils/memory_layers.py`.
    cfg["layers"] = resolve_layers(cfg["layers"])
    cfg.setdefault("collections", {})
    cfg.setdefault("deny_prefixes", [])
    cfg.setdefault("deny_segments", [])
    _fold_in_shared_denies(cfg)
    # R7 ranking knobs: recency x importance x relevance combiner over the RRF
    # candidate set. Defaults ship per plan (Park et al.-style weighting);
    # tune from observed recall. Missing sub-keys are filled defensively.
    cfg.setdefault("rank_weights", {})
    weights = cfg["rank_weights"] if isinstance(cfg["rank_weights"], dict) else {}
    weights.setdefault("semantic", 0.60)
    weights.setdefault("recency", 0.20)
    weights.setdefault("importance", 0.20)
    cfg["rank_weights"] = weights
    cfg.setdefault("recency_decay", "exponential")
    cfg.setdefault("recency_halflife_days", 180)
    # Chunking knobs (point 2). Missing block / sub-keys filled defensively;
    # an empty enabled_layers list means "chunk nothing" (one row per file).
    cfg.setdefault("chunk", {})
    chunk = cfg["chunk"] if isinstance(cfg["chunk"], dict) else {}
    chunk.setdefault("enabled_layers", [])
    chunk.setdefault("max_chars", 700)
    chunk.setdefault("overlap", 120)
    chunk.setdefault("max_chunks", 12)
    # The ceiling every embed text obeys, whatever layer or source produced
    # it. Defaulted rather than required so a config predating the key keeps
    # working, and defaulted to the SAFE value: an absent key used to mean "no
    # bound at all", which is the state that failed hourly for 34 hours.
    chunk.setdefault("embed_max_chars", EMBED_MAX_CHARS_DEFAULT)
    cfg["chunk"] = chunk
    return cfg


# ============================================================
# Store (SQLite)
# ============================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id        TEXT PRIMARY KEY,
    path      TEXT NOT NULL,
    title     TEXT,
    layer     TEXT NOT NULL,
    ntype     TEXT,
    mtime     REAL NOT NULL,
    dim       INTEGER NOT NULL,
    body      TEXT,
    created    TEXT,
    updated    TEXT,
    confidence TEXT,
    status     TEXT,
    classification TEXT,
    chunk     INTEGER NOT NULL DEFAULT 0,
    access_count INTEGER NOT NULL DEFAULT 0,
    last_accessed TEXT,
    written_by TEXT,
    embedding BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    val TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(id UNINDEXED, body);
"""


def open_store(root: Path, store_rel: str | Path) -> sqlite3.Connection:
    """Open `root / store_rel`; `store_rel` may be absolute (`_store_targets` passes
    `content_store_path(root)`, which is `root / content_store_rel(root)` with its directory made)."""
    store_path = root / store_rel
    store_path.parent.mkdir(parents=True, exist_ok=True)
    # `timeout` is what stops the read path dying under the nightly build. Every
    # open runs schema DDL and the migrations below, so a query IS a writer, and
    # with the default 5s a recall hook firing during a per-file build commit got
    # `database is locked` -- an uncaught traceback on the one path this file
    # insists must still answer.
    # No separate `PRAGMA busy_timeout` here: `connect(timeout=)` sets it
    # (measured, 30.0 -> 30000ms), so the PRAGMA was a no-op that read like a
    # second control.
    conn = sqlite3.connect(str(store_path), timeout=SQLITE_BUSY_TIMEOUT_S)
    conn.executescript(SCHEMA)
    # Migrate a pre-hybrid store: add the body column the FTS channel reads from.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(notes)")}
    if "body" not in cols:
        conn.execute("ALTER TABLE notes ADD COLUMN body TEXT")
    # R7 migration (non-destructive, vs scrutiny M2's drop+recreate): an existing
    # index gains created/updated/confidence/status as NULL columns, so query
    # never hits a stale-schema error. NULLs fall back to mtime (recency) and
    # medium (importance) in the combiner; `build --force` repopulates them from
    # frontmatter. Preserving the embeddings avoids a needless full re-embed.
    for _col in ("created", "updated", "confidence", "status"):
        if _col not in cols:
            conn.execute(f"ALTER TABLE notes ADD COLUMN {_col} TEXT")
    # Classification migration (non-destructive, same pattern as R7): an existing
    # index gains a NULL `classification` column; `build` repopulates it from the
    # workspace resolver. Lets query scope corporate vs ceo-only and keeps the
    # schema exec-safe-by-construction. The whole index stays ceo-only regardless.
    if "classification" not in cols:
        conn.execute("ALTER TABLE notes ADD COLUMN classification TEXT")
    # Chunking migration (point 2): an existing index gains a `chunk` column
    # defaulting to 0, so every pre-chunk row reads as chunk 0 (id == path) until
    # `build` re-chunks the long-form layers. Non-destructive, like the above.
    if "chunk" not in cols:
        conn.execute("ALTER TABLE notes ADD COLUMN chunk INTEGER NOT NULL DEFAULT 0")
    # Access-tracking migration (Gap #2, non-destructive, same pattern as above):
    # an existing index gains access_count (default 0) and last_accessed (NULL).
    # `build` repopulates both from frontmatter; `memory-touch.py` bumps them
    # in place on the source file, and the next build picks the new values up.
    if "access_count" not in cols:
        conn.execute("ALTER TABLE notes ADD COLUMN access_count INTEGER NOT NULL DEFAULT 0")
    if "last_accessed" not in cols:
        conn.execute("ALTER TABLE notes ADD COLUMN last_accessed TEXT")
    # Machine-origin migration (HOS-192, same pattern): NULL until a build reads
    # the file, which `_refresh_written_by` does for every NULL row without
    # re-embedding anything.
    if "written_by" not in cols:
        conn.execute("ALTER TABLE notes ADD COLUMN written_by TEXT")
    return conn


def upsert_note(conn, *, id_, path, title, layer, ntype, mtime, body, vec,
                created="", updated="", confidence="", status="",
                classification="", chunk=0, access_count=0, last_accessed="",
                written_by="") -> None:
    blob = np.asarray(vec, dtype=np.float32).tobytes()
    conn.execute(
        """
        INSERT INTO notes (id, path, title, layer, ntype, mtime, dim, body,
                           created, updated, confidence, status, classification,
                           chunk, access_count, last_accessed, written_by, embedding)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            path=excluded.path, title=excluded.title, layer=excluded.layer,
            ntype=excluded.ntype, mtime=excluded.mtime, dim=excluded.dim,
            body=excluded.body, created=excluded.created, updated=excluded.updated,
            confidence=excluded.confidence, status=excluded.status,
            classification=excluded.classification, chunk=excluded.chunk,
            access_count=excluded.access_count, last_accessed=excluded.last_accessed,
            written_by=excluded.written_by,
            embedding=excluded.embedding
        """,
        (id_, path, title, layer, ntype, mtime, len(vec), body,
         created, updated, confidence, status, classification, chunk,
         access_count, last_accessed, written_by, blob),
    )


def resync_fts(conn) -> None:
    """Rebuild the FTS5 channel from the notes table (cheap; runs every build).

    A full re-index of the short snippets is milliseconds and sidesteps per-row
    FTS upsert/prune bookkeeping: after the dense pass settles `notes` (embeds,
    prunes stale, migrates), the lexical channel is derived deterministically
    from it, so the two channels can never drift.
    """
    conn.execute("DELETE FROM notes_fts")
    rows = conn.execute("SELECT id, COALESCE(body, '') FROM notes").fetchall()
    # Prepend humanized path tokens so folder/file names are BM25-searchable.
    # This touches the LEXICAL channel only -- notes.body (the dense embed text)
    # is left clean, so the semantic vector is never diluted by path words.
    conn.executemany(
        "INSERT INTO notes_fts(id, body) VALUES (?, ?)",
        [(id_, f"{humanize_path(id_)} {body}") for id_, body in rows],
    )


# ============================================================
# Indexer (build)
# ============================================================

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def humanize_path(rel: str) -> str:
    """Path -> searchable tokens for the LEXICAL (BM25) channel ONLY.

    Strips the chunk suffix (`path#N`) and a trailing `.md`, turns `/ - _` into
    spaces, collapses whitespace -- so a folder / project / client name that lives
    only in the PATH (e.g. 'Meridian') becomes a BM25-matchable token. NEVER added
    to the dense embed text: that would dilute the semantic vector. All path
    segments are kept (no stop-word / segment-skip rule): generic directory words
    like 'outputs' or 'datastore' are caught by the existing SPARSE_COS_MARGIN
    convergence gate (a BM25-only hit must sit within 0.05 cosine of threshold),
    so they cannot leak through as noise. Do not "optimise" by stripping segments.
    """
    base = rel.split("#", 1)[0]
    if base.endswith(".md"):
        base = base[:-3]
    return re.sub(r"\s+", " ", re.sub(r"[/_-]", " ", base)).strip()


def expand_brace_glob(pattern: str):
    """Expand a single/nested {a,b,c} brace group into concrete glob patterns.

    pathlib/glob do not support brace expansion; the config uses it
    (e.g. odin-brain/{principles,positions,sources}/*.md).
    """
    m = re.search(r"\{([^{}]*)\}", pattern)
    if not m:
        return [pattern]
    pre, post = pattern[: m.start()], pattern[m.end() :]
    results = []
    for option in m.group(1).split(","):
        results.extend(expand_brace_glob(pre + option + post))
    return results


def _would_truncate(body: str, chunk_cfg: dict) -> bool:
    """True only when the cap actually DROPS text, not when it is filled exactly.

    `chunk_text` truncates at `max_chunks`, so `len(pieces) >= max_chunks` was
    the same test as `== max_chunks` and a file that fit the cap perfectly was
    reported as a drop. Re-chunking with one extra slot answers the real
    question: would there have been more?
    """
    probe = chunk_text(
        body,
        max_chars=chunk_cfg["max_chars"],
        overlap=chunk_cfg["overlap"],
        max_chunks=chunk_cfg["max_chunks"] + 1,
    )
    return len(probe) > chunk_cfg["max_chunks"]


def chunk_text(body: str, *, max_chars, overlap, max_chunks):
    """Split body into >=1 overlapping chunks of ~max_chars, paragraph-aware.

    A short body (<= max_chars) returns a single chunk (today's behaviour). Longer
    bodies are packed on blank-line paragraph boundaries; a paragraph longer than
    max_chars is hard-split with `overlap` carried between slices. When a new chunk
    starts, it is seeded with the previous chunk's `overlap` trailing chars for
    continuity. Capped at max_chunks -- the tail is dropped (the cap bounds embed
    cost on huge extract tables; cmd_build logs the drop).
    """
    text = (body or "").strip()
    if not text:
        return [""]
    if len(text) <= max_chars:
        return [text]
    paras = re.split(r"\n\s*\n", text)
    chunks, cur = [], ""
    for p in paras:
        p = p.strip()
        if not p:
            continue
        if len(p) > max_chars:
            if cur:
                chunks.append(cur)
                cur = ""
            step = max(1, max_chars - overlap)
            for i in range(0, len(p), step):
                chunks.append(p[i:i + max_chars])
            continue
        if cur and len(cur) + 1 + len(p) > max_chars:
            chunks.append(cur)
            cur = (cur[-overlap:] + "\n" + p) if overlap else p
        else:
            cur = (cur + "\n" + p) if cur else p
    if cur:
        chunks.append(cur)
    return chunks[:max_chunks] if chunks else [text[:max_chars]]


def bound_embed_texts(texts, chunk_cfg: dict):
    """Split any text past `chunk.embed_max_chars`. Returns (texts, n_split).

    The LAST thing every embed text passes through, whatever layer and whatever
    source produced it, because the bound it enforces is a fact about the
    server and not about any one layer's shape.

    It exists because the bound that was supposed to be here lived in two
    places that between them did not cover the corpus. A glob layer outside
    `chunk.enabled_layers` is bounded by `parse_note`, which cuts the body to
    `SNIPPET_CHARS`; a layer inside it is bounded by `chunk_text`. The
    `git-log` and `codegraph` sources call NEITHER -- they build `embed_text`
    themselves in `scripts/utils/commit_source.py` and `scripts/utils/symbol_source.py`
    and hand it straight to the unchunked branch -- so their rows had no bound
    at all while the build printed "N hit chunk cap" and read as enforced.
    MEASURED 2026-09-24 over the 1229 engine commits on this branch: the
    largest embed text was 89,834 characters, 81,054 of them the appended list
    of 1,550 changed paths, and the embed server returned HTTP 500 on it every
    hour for 34 hours.

    Splitting rather than truncating, with `chunk_text` rather than a second
    splitter, so an oversized tail stays retrievable as its own row and the
    caller's existing `path#N` numbering absorbs it. `max_chunks` still bounds
    the total, so a pathological input cannot turn into unbounded embed cost.

    The final slice is not belt-and-braces. `chunk_text` packs paragraphs and
    seeds a new chunk with `overlap` trailing characters of the previous one,
    so a returned chunk may exceed `max_chars` by up to `overlap + 1` -- 121
    characters at today's settings, harmless at 700 and the difference between
    passing and a 500 when `max_chars` IS the ceiling. The characters it cuts
    are the overlap seed, which the neighbouring chunk already carries.
    """
    ceiling = chunk_cfg["embed_max_chars"]
    out, split = [], 0
    for text in texts:
        if len(text) <= ceiling:
            out.append(text)
            continue
        split += 1
        pieces = chunk_text(
            text,
            max_chars=ceiling,
            overlap=chunk_cfg["overlap"],
            max_chunks=chunk_cfg["max_chunks"],
        )
        out.extend(piece[:ceiling] for piece in pieces)
    return out, split


def add_pending(pending, base_id, rel, layer, mtime, info, embed_texts, chunk_cfg):
    """Queue one record's rows for embedding, bounded. Returns n_split.

    The ONE door into `pending`, and that is the whole point of it existing.
    `_build_store` walks three kinds of source -- a glob, `codegraph` symbols
    and `git-log` commits -- and each used to append its own rows in its own
    branch. So a bound added to one branch covered a third of the corpus, which
    is exactly the state this repairs: the glob branch's texts were bounded
    twice over, by `parse_note` and by `chunk_text`, while the other two handed
    an unbounded `embed_text` straight through and killed the build at the
    server. A fourth source is a fourth branch, and it will reach this function
    because there is nowhere else to reach.

    `base_id` is the row identity a source assigns (`rel` for a glob, a
    `commit:<label>:<sha>` or a symbol id otherwise). Chunk 0 keeps it
    unchanged, so nothing that was one row and still fits becomes a different
    row; a split appends `#N`, the numbering the glob branch already used.
    """
    bounded, split = bound_embed_texts(embed_texts, chunk_cfg)
    for idx, etext in enumerate(bounded):
        cid = base_id if idx == 0 else f"{base_id}#{idx}"
        pending.append((cid, rel, layer, idx, mtime, info, etext))
    return split


def parse_note(text: str) -> dict:
    """Parse a markdown note. Returns a dict with title, ntype, embed_text and
    the R7 ranking inputs (created, updated, confidence, status) from frontmatter.

    All Odin notes carry created+updated (YYYY-MM-DD) + confidence
    (high/medium/low); positions/sources carry status: evergreen. Missing fields
    return "" - the combiner applies fallbacks (mtime for recency, medium for
    importance).
    """
    title, ntype = "", ""
    created = updated = confidence = status = ""
    access_count = 0
    last_accessed = ""
    written_by = ""
    body = text
    fm = FRONTMATTER_RE.match(text)
    if fm:
        body = text[fm.end() :]
        try:
            meta = yaml.safe_load(fm.group(1)) or {}
            if isinstance(meta, dict):
                title = str(meta.get("title", "") or "")
                ntype = str(meta.get("type", "") or "")
                created = str(meta.get("created", "") or "")
                updated = str(meta.get("updated", "") or "")
                confidence = str(meta.get("confidence", "") or "")
                status = str(meta.get("status", "") or "")
                # access_count/last_accessed (Gap #2): auto-memory's real
                # frontmatter nests these under `metadata:` (confirmed against
                # a real auto-memory file), so a top-level key wins when it
                # carries a VALUE, falling back to the nested `metadata.*` form.
                #
                # "Carries a value" is the load-bearing word. `dict.get(k, d)`
                # falls back only when the key is ABSENT, so a bare
                # `access_count:` (YAML null, an ordinary way to write "no value
                # here") beat a real `metadata.access_count: 5` and the note
                # ranked as never-cited. The two lines below now agree: a null
                # top-level key defers to the nested one, and an explicit
                # top-level `0` still wins, because 0 is a value.
                nested = meta.get("metadata")
                nested = nested if isinstance(nested, dict) else {}
                raw_access_count = meta.get("access_count")
                if raw_access_count is None:
                    raw_access_count = nested.get("access_count", 0)
                try:
                    access_count = int(raw_access_count or 0)
                except (TypeError, ValueError):
                    access_count = 0
                last_accessed = str(
                    meta.get("last_accessed", "") or nested.get("last_accessed", "") or ""
                )
                # HOS-192: who wrote the record. The distiller nests it under
                # `metadata:`, the same as the two fields above.
                written_by = str(
                    meta.get("written_by", "") or nested.get("written_by", "") or ""
                )
        except yaml.YAMLError:
            pass
    if not title:
        h1 = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
        if h1:
            title = h1.group(1).strip()
    snippet = re.sub(r"\s+", " ", body).strip()[:SNIPPET_CHARS]
    embed_text = f"{title}\n{snippet}".strip() if title else snippet
    return {
        "title": title, "ntype": ntype, "embed_text": embed_text,
        "raw_body": body,   # frontmatter-stripped, newline-preserving (for chunk_text)
        "created": created, "updated": updated,
        "confidence": confidence, "status": status,
        "access_count": access_count, "last_accessed": last_accessed,
        "written_by": written_by,
    }


def _store_targets(cfg, *, create: bool = False):
    """The physical-store plan: one entry per store, each on its own side of the
    engine/data seam.

    The DATA store (`content_store_path(get_data_root())`) physically holds every
    NON-code collection's layers -- `content` plus any DATA-side collection such
    as `chronicle`. The `code` collection (skill/rule) builds from the ENGINE root
    (`get_workspace_root()`) into CODE_STORE_REL. Store membership only decides
    WHERE rows live; query-time `--collection` still scopes RESULTS per collection
    via the `allowed` layer filter, so a DATA-side collection (chronicle) can share
    the physical store with `content` yet stay out of the default `content` result
    set. Returns a list of dicts {name, root, store_rel, layers(set), tree}. The roots
    are resolved here (not at import) so tests can monkeypatch
    get_data_root/get_workspace_root on this module. A configured layer that
    belongs to no collection is reported once and simply not built (it would
    otherwise silently route nowhere)."""
    colls = cfg.get("collections") or {}
    if colls:
        code_layers = set(colls.get("code", []))
        # DATA store = union of all non-code collections (content, chronicle, ...).
        data_layers = set()
        for _name, _lyrs in colls.items():
            if _name == "code":
                continue
            data_layers.update(_lyrs)
    else:
        # Back-compat: no collections map -> every layer builds into the single
        # content store (the pre-split behaviour; keeps minimal configs working).
        data_layers = {lc["layer"] for lc in cfg.get("layers", [])}
        code_layers = set()
    # `tree` names which side of the seam the store's paths are RELATIVE to,
    # for the classification resolver: the content store's paths are data-root
    # relative, the code store's are engine-root relative.
    data_root = get_data_root()
    targets = [{
        "name": "content", "root": data_root,
        "store_rel": content_store_path(data_root, create=create),
        "layers": data_layers, "tree": "data",
    }]
    if code_layers:
        targets.append({
            "name": "code", "root": get_workspace_root(),
            "store_rel": CODE_STORE_REL, "layers": code_layers, "tree": "engine",
        })
    covered = data_layers | code_layers
    orphan = [lc["layer"] for lc in cfg.get("layers", []) if lc["layer"] not in covered]
    if orphan:
        sys.stderr.write(
            f"{YELLOW}warn:{RESET} layers in no collection (not built/queried): "
            f"{', '.join(sorted(set(orphan)))}\n"
        )
    return targets


def _layer_store_map(cfg):
    """layer -> (root, store_rel) for query routing. A layer appearing in two
    collections is an ambiguous route and raises (config error)."""
    m = {}
    for t in _store_targets(cfg):
        for lyr in t["layers"]:
            if lyr in m:
                raise ValueError(
                    f"layer '{lyr}' is in two stores; collection routing is ambiguous"
                )
            m[lyr] = (t["root"], t["store_rel"])
    return m


def cmd_build(args) -> int:
    # memory-index.yaml is engine config. The flag travels into resolution: with
    # it, an unreachable pin degrades to the local daemon (loudly); without it,
    # resolution leaves `host` None and the refusal below fires.
    cfg = load_config(
        get_workspace_root(),
        allow_fallback=bool(getattr(args, "allow_host_fallback", False)),
    )

    # A build writes vectors that outlive the run. Embedding a few thousand of
    # them on an unintended host is precisely the split brain the operator asked
    # to prevent, and nothing downstream would ever surface it -- cosine compares
    # mismatched vectors happily and returns a plausible number.
    if cfg.get("host") is None:
        # load_config already printed the red banner naming what was tried.
        sys.stderr.write(
            f"{RED}{BOLD}Refusing to build: the pinned embedder is down.{RESET}\n"
        )
        return 1

    targets = _store_targets(cfg, create=True)
    rc = 0
    for t in targets:
        # One writer per store at a time (see `store_lock`).
        with store_lock(t["root"], t["store_rel"], what="build"):
            store_rc = _build_store(
                cfg, t["root"], t["store_rel"], t["layers"], args.force,
                tree=t.get("tree"),
            )
        rc = rc or store_rc
    return rc


# The whole companion pass is bounded so the build after it always runs. A full
# re-embed took 1 h 49 min on 2026-09-24, so this bounds extraction only.
EXTRACT_SECONDS = 1800


def cmd_refresh(args) -> int:
    """The hourly job: document companions first, then an incremental build.

    A document is findable only through its `-extract.md` companion, so the
    companions are written first (HOS-149). Until HOS-194 that pass was the
    unit's `ExecStartPre=-...`, and the leading `-` made systemd ignore its exit
    code. MEASURED 2026-10-03: the pass exited 1 every hour on a deliberately
    corrupt test fixture while the unit reported `Result=success`, so the
    failure lived only in a journal line nobody reads. Here the build still
    runs whatever the pass returns, and the job's own exit code carries the
    failure of either half.
    """
    script = Path(__file__).resolve().parent / "datastore-extract.py"
    try:
        extract_rc = subprocess.run(
            [str(venv_python()), str(script)], timeout=EXTRACT_SECONDS,
        ).returncode
    except subprocess.TimeoutExpired:
        sys.stderr.write(
            f"{RED}Companion extraction did not finish in {EXTRACT_SECONDS} s and "
            f"was stopped; what it wrote is kept.{RESET}\n")
        extract_rc = None
    build_rc = cmd_build(argparse.Namespace(force=False, allow_host_fallback=False))
    if extract_rc != 0:
        how = "timed out" if extract_rc is None else f"exited {extract_rc}"
        sys.stderr.write(
            f"{RED}{BOLD}refresh FAILED: companion extraction {how}; the index "
            f"build ran and exited {build_rc}.{RESET}\n")
        return build_rc or 1
    return build_rc


def host_fell_back(preferred: str | None, resolved: str) -> bool:
    """True when the operator asked for a host and got a different one.

    `resolve_inference_host` returns only the winner, so the drop is invisible to a
    caller that did not keep the raw config value. A build needs to know: writing
    a few thousand vectors from an unintended embedder is the split brain, and it
    is silent unless someone compares the two hosts by hand.
    """
    if not _pin_entries(preferred):
        return False                     # nothing asked for, nothing denied
    return _answering_entry(preferred, resolved) is None


def _pin_entries(preferred) -> list:
    entries = preferred if isinstance(preferred, (list, tuple)) else [preferred]
    return [e.strip() for e in entries if isinstance(e, str) and e.strip()]


def _answering_entry(preferred, resolved: str) -> str | None:
    """The entry of the configured pin that `resolved` came from, or None."""
    landed = resolved.rstrip("/")
    for want in _pin_entries(preferred):
        if want.startswith("auto"):
            _, _, port = want.partition(":")
            # The local daemon is excluded explicitly: since the pin may name
            # 11434 - the port the Windows desktop app actually binds - a bare
            # suffix match would call `localhost:11434` a successful resolution
            # of `auto:11434`, which is the exact substitution being guarded.
            if port and landed.endswith(f":{port}") and landed != LOCAL_HOST:
                return want
        elif want.rstrip("/") == landed:
            return want.rstrip("/")
    return None


def embed_pin(preferred, resolved: str) -> str:
    """Which configured SERVER answered: the pin entry as written, else the address.

    `auto:13305` survives a WSL gateway address change, which is the point; a
    literal address in the pin, such as a VM's, changes when the server does.
    With no pin, or on an explicit fallback, the resolved address is the answer.
    Stamped as `embed_pin` and compared by `embedder_change` (HOS-194).
    """
    return _answering_entry(preferred, resolved) or resolved.rstrip("/")


def host_fallback_info(preferred: str | None, resolved: str) -> dict | None:
    """The fallback as data, for a caller that renders instead of printing.

    `recall-inject.py` captures stderr and discards it on a zero exit, so the
    banner below never reaches the session -- the one surface the operator reads
    all day. The JSON carries the same fact so the hook can show it.
    """
    if not host_fell_back(preferred, resolved):
        return None
    return {"wanted": preferred, "got": resolved}


def host_fallback_banner(preferred: str | None, resolved: str, *,
                         remedy: str | None = None) -> str | None:
    """Loud, red, immediate. None when the preferred host answered.

    Operator directive, 2026-08-21: *"if the Windows system (where the GPU is) is
    unavailable, you say so AT ONCE, loudly, in red"* (translated). Before this the
    drop was silent by construction -- `resolve_inference_host` returns the winner
    and says nothing about who lost, so a sleeping Windows box simply produced a
    slower run that looked identical to a fast one.

    The banner reports the drop; it does not decide what to do about it. A build
    refuses (see `cmd_build`), a query proceeds -- the two hosts run the same
    `bge-m3` digest and agree to cosine 0.99997, measured 2026-08-21, so blocking
    recall would trade a live capability for float noise.

    `remedy` is `embed_host_remedy`'s sentence. Until HOS-215 this line read
    "Start Ollama on the Windows host" whatever the pin declared.
    """
    info = host_fallback_info(preferred, resolved)
    if info is None:
        return None
    bar = "=" * 70
    return (
        f"\n{RED}{BOLD}{bar}\n"
        f"  PINNED EMBEDDER NOT AVAILABLE -- RUNNING ON THE FALLBACK HOST\n"
        f"{bar}{RESET}\n"
        f"{RED}{BOLD}  wanted:{RESET}{RED} {info['wanted']}{RESET}\n"
        f"{RED}{BOLD}  got:   {RESET}{RED} {info['got']}{RESET}\n"
        f"{YELLOW}  {remedy or embed_host_remedy(provider='', host=preferred)}{RESET}\n"
        f"{RED}{BOLD}{bar}{RESET}\n"
    )


def _remedy_lines(remedy: str) -> str:
    """`embed_host_remedy`'s sentence as banner lines, with the fallback hint."""
    return (
        f"{YELLOW}  {remedy}{RESET}\n"
        f"{YELLOW}  A rebuild on the local daemon needs the explicit "
        f"--allow-host-fallback.{RESET}\n"
    )


def embedder_down_banner(reason: str, *, remedy: str | None = None) -> str:
    """Loud, red, immediate — nothing can be embedded, and here is why.

    Sibling of `host_fallback_banner`, and the one that fires under the
    2026-08-23 policy. The other banner now appears only behind the explicit
    `--allow-host-fallback`.

    `remedy` exists because this banner gained a second cause on 2026-09-23 and
    the advice it carried is true of only the first. An unreachable host is
    repaired by starting the host; a key NAMED in config and absent from the
    environment is repaired by setting one variable, and telling that operator
    to open the Ollama tray app sends him to a machine that is working fine.
    With no remedy given the banner says the serving program is unknown: until
    HOS-215 the default named the Windows-side Ollama whatever the pin declared,
    and sent the operator to Ollama while the pin was Lemonade Server.
    """
    bar = "=" * 70
    if remedy is None:
        remedy = _remedy_lines(embed_host_remedy(provider="", host=None))
    return (
        f"\n{RED}{BOLD}{bar}\n"
        f"  EMBEDDER NOT AVAILABLE -- NOTHING WILL BE EMBEDDED\n"
        f"{bar}{RESET}\n"
        f"{RED}{BOLD}  {reason}{RESET}\n"
        f"{remedy}"
        f"{RED}{BOLD}{bar}{RESET}\n"
    )


def embed_unavailable_payload(reason: str, remedy: str | None = None) -> dict:
    """The refusal as JSON, for `query --json`.

    `.claude/hooks/recall-inject.py` captures stderr and discards it, so a red
    banner never reaches the session — the surface the operator actually reads.
    The same fact has to travel as data or it does not travel. Shaped like a gap
    so a naive consumer degrades to "no memory" rather than crashing, with
    `embed_unavailable` present for one that knows better.

    `remedy` is what to start and where, from `embed_host_remedy`; the hook
    prints it rather than naming a program of its own (HOS-215).
    """
    down = {"reason": reason}
    if remedy:
        down["remedy"] = remedy
    return {
        "hits": [],
        "gap": True,
        "embed_unavailable": down,
    }


def record_provenance(conn, *, model: str, host: str, digest: str | None = None,
                      provider: str | None = None, pin: str | None = None) -> None:
    """Stamp WHICH embedder built this store, so split brain is detectable.

    Vectors from two models are not comparable and cosine gives no hint -- it
    returns a plausible number either way. Before this the `meta` table held the
    model name alone, so "was this store built by one embedder?" had no answer on
    disk and could only be reached by re-running both hosts and comparing.

    The digest is stamped only when the host answered with one. An unknown digest
    leaves the old value alone rather than overwriting it with a blank, so a
    momentarily unreachable tags endpoint cannot erase the evidence.

    `provider` is the PROGRAM that served the vectors, and it is here because
    the digest stopped being sufficient on 2026-09-23. Lemonade Server returns
    a constant `sha256:00000...` for every model it holds, so
    `scripts/utils/embeddings.model_digest` now reports that as unknown rather
    than as a value -- which is honest, and which leaves the store with nothing
    at all identifying its producer unless the producer is named. It is stamped
    on exactly the same terms as the digest: only when known, never overwriting
    a known value with a blank.
    """
    if provider:
        conn.execute(
            "INSERT INTO meta (key, val) VALUES ('embed_provider', ?) "
            "ON CONFLICT(key) DO UPDATE SET val=excluded.val",
            (provider,),
        )
    if pin:
        conn.execute(
            "INSERT INTO meta (key, val) VALUES ('embed_pin', ?) "
            "ON CONFLICT(key) DO UPDATE SET val=excluded.val",
            (pin,),
        )
    for key, val in (("model", model), ("embed_host", host)):
        conn.execute(
            "INSERT INTO meta (key, val) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET val=excluded.val",
            (key, val),
        )
    if digest:
        conn.execute(
            "INSERT INTO meta (key, val) VALUES ('model_digest', ?) "
            "ON CONFLICT(key) DO UPDATE SET val=excluded.val",
            (digest,),
        )


MIXED_PROVENANCE_KEY = "mixed_provenance"


def _embedder_id(model: str, host: str, digest: str | None,
                 provider: str | None = None) -> str:
    # The provider is prefixed rather than appended because it is the field
    # that survives when the digest does not: a host that answers with a
    # placeholder digest leaves `(...)` off entirely, and "the store was mixed
    # by SOMETHING at this address" is not an identification.
    name = f"{provider}/{model}" if provider else model
    return f"{name} @ {host}" + (f" ({digest[:12]})" if digest else "")


def record_mixed_provenance(conn, *, model: str, host: str, digest: str | None = None,
                            provider: str | None = None) -> None:
    """Flag the store as holding vectors from more than one embedder.

    Sticky, and binary: it names the first embedder mixed in, and is not a log.

    `record_provenance` used to run unconditionally at the end of every build,
    two dozen lines below a warning that says the unchanged rows keep their old
    vectors. So an incremental build on a second embedder re-embedded the changed
    files, stamped ITS identity over the meta row, and from the next run onward
    `provenance_mismatch` compared the new stamp against the same embedder and
    returned None. The store was mixed and the only on-disk evidence of the mix
    had been erased by the code that printed the warning. Nothing clears this but
    a whole-store `build --force`.
    """
    row = conn.execute(
        "SELECT val FROM meta WHERE key=?", (MIXED_PROVENANCE_KEY,)
    ).fetchone()
    if row and row[0]:
        return                          # already flagged; the flag is binary
    conn.execute(
        "INSERT INTO meta (key, val) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET val=excluded.val",
        (MIXED_PROVENANCE_KEY, _embedder_id(model, host, digest, provider)),
    )


def clear_mixed_provenance(conn) -> None:
    """Drop the mixed flag. Only a build that re-embedded EVERY row may call this."""
    conn.execute("DELETE FROM meta WHERE key=?", (MIXED_PROVENANCE_KEY,))


def provenance_mismatch(conn, *, model: str, host: str, digest: str | None = None,
                        provider: str | None = None) -> str | None:
    """One line naming the drift, or None. Never raises, never blocks.

    Three fields, three different meanings:

    - **model name** -- a change genuinely breaks cosine and must be loud.
    - **host** -- float noise with the same weights (measured 2026-08-21 at cosine
      0.99997 between the Windows GPU and WSL CPU hosts), still reported, because
      the operator asked to run on one host and a silent fallback is how that
      stops being true.
    - **digest** -- the one that answers the operator's 2026-08-21 question, "how will
      sync between the instances work?" (translated). Nothing syncs the two Ollama installs, so an
      auto-update on one host swaps the weights under an unchanged tag: same name
      `bge-m3`, different vectors, and no other field moves. The digest is the
      only evidence that happened.
    - **provider** -- which PROGRAM served the vectors. Until 2026-10-03 it was
      reported and not refused, on a measurement: in HELM on 2026-09-23
      Lemonade and ollama returned the same 1024 numbers for the same bge-m3,
      cosine 1.000000 on three texts. That let both local stores fill with rows
      from a second server under the first one's stamp, and the operator
      decided on 2026-10-03 (HOS-194) to guard against mixing. This function
      still only REPORTS; `embedder_change` is the half that refuses.

    A missing stored digest is silence, not a match -- a store built before this
    field existed cannot be judged, and saying "same" would be a claim the data
    does not carry. The same applies to a missing provider, and to a digest that
    arrived as a placeholder: `scripts/utils/embeddings.model_digest` returns
    None for those, so they reach here as unknown rather than as a value that
    would match everything it was ever compared against.

    A fourth field, `mixed_provenance`, is not a comparison at all: it is a
    sticky record that a past build already wrote rows on a second embedder. It
    reports on every run regardless of what the current embedder is, because the
    mix is a property of the stored vectors and no live comparison can see it.
    """
    try:
        meta = dict(conn.execute("SELECT key, val FROM meta"))
    except sqlite3.Error:
        return None
    if not meta.get("model") and not meta.get("embed_host"):
        return None                      # store predates this field
    drift = []
    if meta.get(MIXED_PROVENANCE_KEY):
        drift.append(
            f"MIXED: rows embedded by {meta[MIXED_PROVENANCE_KEY]} were added to a "
            f"store stamped {meta.get('model', '?')} @ {meta.get('embed_host', '?')} "
            f"-- only `build --force` clears this"
        )
    if meta.get("model") and meta["model"] != model:
        drift.append(f"model {meta['model']} -> {model}")
    if provider and meta.get("embed_provider") and meta["embed_provider"] != provider:
        drift.append(f"provider {meta['embed_provider']} -> {provider}")
    if meta.get("embed_host") and meta["embed_host"] != host:
        drift.append(f"host {meta['embed_host']} -> {host}")
    if digest and meta.get("model_digest") and meta["model_digest"] != digest:
        drift.append(
            f"WEIGHTS CHANGED: digest {meta['model_digest'][:12]} -> {digest[:12]} "
            f"(same tag, different model -- the stored vectors are not comparable)"
        )
    if not drift:
        return None
    return "built by a different embedder: " + "; ".join(drift)


def _endpoint(url: str) -> tuple:
    """What about an embed host's address identifies the SERVER, not the route to it.

    Scheme, port, and whether the host is this machine. Not the address itself:
    an `auto:<port>` pin follows the current WSL gateway, whose address changes
    across WSL restarts (see `load_config`), and an address change alone would
    otherwise refuse every hourly build after a reboot until a full re-embed.
    The cost, stated rather than hidden: two DIFFERENT remote machines serving
    on the same port read as one endpoint here, and only the model and digest
    checks can tell them apart.
    """
    parsed = urlparse(url if "://" in url else f"http://{url}")
    name = (parsed.hostname or "").lower()
    where = "loopback" if name in ("localhost", "127.0.0.1", "::1") else "remote"
    return (parsed.scheme, where, parsed.port)


def embedder_change(conn, *, model: str, host: str, digest: str | None = None,
                    provider: str | None = None, pin: str | None = None) -> str | None:
    """Why the current embedder is not the one this store is stamped with, or None.

    The REFUSING half of `provenance_mismatch`, and the reason it exists is a
    measurement. MEASURED 2026-10-03 (HOS-194): both local stores held rows from
    `lemonade/bge-m3` on port 13305 inside stores stamped `bge-m3` on port 11434,
    because the machine's embed pin moved from one server to the other and every
    incremental build since only WARNED, then wrote. Operator decision of
    2026-10-03 (HOS-194): guard against mixing. An incremental build adds rows
    beside the stored ones, so it may only run on the embedder that wrote them;
    `build --force` re-embeds the whole store and is the one way to change it.

    Compared: the model name, the provider and the digest when BOTH sides know
    them (an unknown is silence, as in `provenance_mismatch`), and the endpoint
    by `_endpoint`. A store with no stamp yet has nothing to mismatch.

    And the pin entry (`embed_pin`), because `_endpoint` alone reads two remote
    machines on one port as one server: a move to a VM on the same port would
    pass it, and Lemonade reports no digest that could catch it instead. The pin
    is the operator's own name for the server, so it changes exactly when the
    server does. A store stamped before the pin existed is judged by the
    endpoint only.
    """
    try:
        meta = dict(conn.execute("SELECT key, val FROM meta"))
    except sqlite3.Error as exc:
        sys.stderr.write(f"{YELLOW}warn:{RESET} could not read the store's "
                         f"embedder stamp ({exc}); nothing to compare.\n")
        return None
    reasons = []
    if meta.get("model") and meta["model"] != model:
        reasons.append(f"model {meta['model']} -> {model}")
    if provider and meta.get("embed_provider") and meta["embed_provider"] != provider:
        reasons.append(f"provider {meta['embed_provider']} -> {provider}")
    if digest and meta.get("model_digest") and meta["model_digest"] != digest:
        reasons.append(f"digest {meta['model_digest'][:12]} -> {digest[:12]}")
    if meta.get("embed_host") and _endpoint(meta["embed_host"]) != _endpoint(host):
        reasons.append(f"endpoint {meta['embed_host']} -> {host}")
    if pin and meta.get("embed_pin") and meta["embed_pin"] != pin:
        reasons.append(f"pin {meta['embed_pin']} -> {pin}")
    return "; ".join(reasons) or None


#: Routing refusal reasons already reported this run, so a store full of
#: commit ids says so once and not once per row.
_ROUTING_REFUSALS_SAID: set[str] = set()


def _say_routing_refusal(exc: RoutingRefused) -> None:
    """Report, once per reason, that a row took the closed label on a refusal."""
    if exc.reason not in _ROUTING_REFUSALS_SAID:
        _ROUTING_REFUSALS_SAID.add(exc.reason)
        sys.stderr.write(
            f"{YELLOW}classification:{RESET} the routing contract refused "
            f"{exc.path!r} ({exc.reason}); such rows are labelled ceo-only, "
            f"the closed label (HOS-232). Said once per reason.\n")


def _refresh_classifications(conn, tree_by_layer, degraded, tree) -> int:
    """Recompute `classification` for every stored row and UPDATE the ones that
    differ. Returns the number of rows corrected.

    Runs on every build, `--force` or not, over the rows the build otherwise
    skips as up-to-date: the column is a function of the routing map and the
    tree, not of the record, so "the record has not changed" says nothing about
    it. Touches `classification` only; `embedding`, `mtime`, `access_count` and
    `last_accessed` are never in the statement. The WHERE names path AND layer,
    because a path is unique within a store only together with its layer.

    `tree_by_layer` is the build's own map (a git-log layer's tree is its
    `repo_label`; a glob or symbol layer's is the store's `tree`); a layer absent
    from it was not walked by this pass, and a layer in `degraded` could not be,
    so both keep their rows as they are.
    """
    refreshed = 0
    rows = conn.execute(
        "SELECT DISTINCT path, layer, classification FROM notes"
    ).fetchall()
    for path, layer, stored in rows:
        if layer in degraded or layer not in tree_by_layer:
            continue
        # A commit id (`engine@<sha>`) or an engine file outside the
        # allowlist has no routing answer since HOS-232; the visibility label
        # then takes its closed value and the run says so.
        try:
            current = get_classification(path, tree=tree_by_layer.get(layer, tree))
        except RoutingRefused as exc:
            _say_routing_refusal(exc)
            current = "ceo-only"
        if current == (stored or ""):
            continue
        cur = conn.execute(
            "UPDATE notes SET classification=? WHERE path=? AND layer=?",
            (current, path, layer),
        )
        refreshed += cur.rowcount
    return refreshed


def _refresh_written_by(conn, root: Path) -> int:
    """Fill `written_by` on rows that have never had it read (HOS-192).

    A row stored before the column existed reads NULL, and the build skips an
    unchanged file by mtime, so without this a record would rank as a person's
    until somebody edited it or ran `build --force`, which re-embeds everything.
    This reads the frontmatter of each NULL row's file once and writes the
    answer, `""` when there is none or the path is not a file (a commit or a
    symbol range), so the next build finds nothing to do. Touches `written_by`
    only.
    """
    filled = 0
    paths = [r[0] for r in conn.execute(
        "SELECT DISTINCT path FROM notes WHERE written_by IS NULL").fetchall()]
    for rel in paths:
        source = root / rel
        writer = ""
        if source.suffix == ".md" and source.is_file():
            try:
                writer = parse_note(source.read_text(encoding="utf-8",
                                                     errors="replace"))["written_by"]
            except OSError as exc:
                print(f"{YELLOW}build:{RESET} written_by unread for {rel}: {exc}",
                      file=sys.stderr)
                continue
        cur = conn.execute(
            "UPDATE notes SET written_by=? WHERE path=? AND written_by IS NULL",
            (writer, rel))
        filled += cur.rowcount
    return filled


def _build_store(cfg, root, store_rel, layers, force, *, tree=None) -> int:
    conn = open_store(root, store_rel)

    # `headers` is passed rather than left to resolve itself, so the credential
    # comes from the SAME root this build reads its config from. The default
    # resolution inside `model_digest` goes through `get_workspace_root()`,
    # which is a different question and is how `index_embed_model` once read a
    # model name out of another clone.
    #
    # `root` is NOT that root and never was: it is the root of the STORE this
    # call writes, which for the content store is the data overlay. Asking it a
    # config question is what sent the content store's build unauthenticated at
    # a key-gated server, 34 hourly failures from 2026-09-23 02:15. The config
    # carries its own root now, so this asks the config.
    headers = config_auth_headers(cfg)
    provider = cfg.get("provider")
    pin = embed_pin(cfg.get("host_preferred"), cfg["host"])
    digest = model_digest(model=cfg["model"], host=cfg["host"], headers=headers)
    drift = provenance_mismatch(conn, model=cfg["model"], host=cfg["host"],
                                digest=digest, provider=provider)
    if drift:
        sys.stderr.write(
            f"{YELLOW}warn:{RESET} {root / store_rel} {drift}\n"
            f"{YELLOW}Rows already stored keep their old vectors. "
            f"Re-run with --force to make the store one provenance.{RESET}\n"
        )
    # Refused before anything is read, pruned or written: an incremental pass on
    # a second embedder is how both stores became mixed (see `embedder_change`).
    change = None if force else embedder_change(
        conn, model=cfg["model"], host=cfg["host"], digest=digest, provider=provider,
        pin=pin)
    if change:
        conn.close()
        sys.stderr.write(
            f"{RED}{BOLD}Refusing to build {root / store_rel}: the current embedder "
            f"is not the one that wrote it ({change}).{RESET}\n"
            f"{YELLOW}Nothing was written. `build --force` re-embeds the whole "
            f"store on the current embedder.{RESET}\n"
        )
        return 1

    deny_prefixes = store_denies(cfg["deny_prefixes"], root, tree)
    deny_segments = cfg["deny_segments"]
    file_denies = file_deny_segments(deny_segments)
    chunk_cfg = cfg["chunk"]
    chunk_layers = set(chunk_cfg["enabled_layers"])

    # Per-file mtime (a file's chunks all share its mtime), keyed by path.
    existing_by_path = {p: m for p, m in conn.execute("SELECT path, mtime FROM notes")}

    claimed = set()        # paths claimed this pass (first layer wins; prune basis)
    changed_paths = set()  # paths to delete-then-reinsert (new / changed / forced)
    pending = []           # (cid, rel, layer, chunk_idx, mtime, info, embed_text)
    skipped_uptodate = 0
    denied_count = 0
    capped = 0
    # Records whose embed text exceeded `chunk.embed_max_chars` and were split
    # to fit. Counted and REPORTED rather than silently reshaped: a row that
    # became four rows is a different thing in the store from the one the
    # config's layer list says it should be, and the reader deserves to know
    # which build did it.
    oversized = 0
    # Files a glob layer listed and that were gone by the time this pass reached
    # them. Counted and NAMED, never a silent narrowing: the summary line below
    # reports what was actually indexed, not what the walk listed.
    vanished = []
    # Layers whose source raised, so their rows were KEPT rather than rebuilt.
    # This pass cannot speak for those vectors, whatever its layer list says.
    degraded = set()
    # The tree each layer's paths are relative to, for `get_classification`.
    # Glob and symbol layers sit under the store's root (`tree`); a commit layer's
    # synthetic `<repo_label>@<sha>` id names its own repository, so its tree is
    # the label, whichever store it is built into.
    tree_by_layer = {}

    for layer_cfg in cfg["layers"]:
        layer = layer_cfg["layer"]
        if layer not in layers:
            continue            # this store builds only its collection's layers

        # A layer's `source` says where its rows come from. Absent means the
        # original glob walk. `git-log` reads commit messages instead, which are
        # not files -- so the two branches share the pending/claimed/prune
        # bookkeeping below and nothing else.
        #
        # Claiming matters more than it looks: the prune at the end of this
        # function deletes every stored path NOT claimed this pass. If a commit
        # branch silently yields nothing, its whole corpus is wiped and the next
        # query answers from an empty layer. So a git failure keeps the layer's
        # existing paths claimed (see the except below) instead of degrading into
        # a delete.
        tree_by_layer[layer] = tree
        if layer_cfg.get("source") == "codegraph":
            graph = root / layer_cfg.get("graph_db", ".codegraph/codegraph.db")
            try:
                walk_stats = {}
                symbols = list(iter_symbols(
                    graph, root,
                    deny_prefixes=tuple(deny_prefixes),
                    deny_segments=tuple(deny_segments),
                    stats=walk_stats,
                ))
                denied_count += walk_stats.get("denied", 0)
            except (ValueError, sqlite3.Error) as exc:
                # Same contract as the commit branch: degrade loudly, keep the
                # corpus. CodeGraph's index is gitignored and rebuildable, so a
                # fresh clone legitimately has none.
                sys.stderr.write(
                    f"{RED}symbol layer {layer} unavailable:{RESET} {exc}\n"
                    f"{YELLOW}Its existing rows are kept, not pruned.{RESET}\n"
                )
                degraded.add(layer)
                claimed.update(
                    p for p, in conn.execute(
                        "SELECT DISTINCT path FROM notes WHERE layer=?", (layer,)
                    )
                )
                continue
            for s in symbols:
                rel = s["path"]
                if rel in claimed:
                    continue
                claimed.add(rel)
                # A symbol's path carries its line range, so a moved symbol is a
                # NEW path and re-embeds; an edited one is caught by its file's
                # mtime, exactly like a glob layer.
                if (
                    not force
                    and rel in existing_by_path
                    and abs(existing_by_path[rel] - s["mtime"]) < 1e-6
                ):
                    skipped_uptodate += 1
                    continue
                changed_paths.add(rel)
                info = {
                    "title": s["title"], "ntype": s["ntype"], "raw_body": s["body"],
                    "embed_text": s["embed_text"], "created": "", "updated": "",
                    "confidence": "", "status": "", "access_count": 0,
                    "last_accessed": "",
                }
                oversized += add_pending(
                    pending, s["id"], rel, layer, s["mtime"], info,
                    [s["embed_text"]], chunk_cfg)
            continue

        if layer_cfg.get("source") == "git-log":
            label = layer_cfg.get("repo_label", "engine")
            tree_by_layer[layer] = label if label in ("data", "engine") else tree
            try:
                walk_stats = {}
                commits = list(iter_commits(
                    root,
                    repo_label=label,
                    include_paths=layer_cfg.get("body_paths", True),
                    deny_prefixes=tuple(deny_prefixes),
                    deny_segments=tuple(deny_segments),
                    stats=walk_stats,
                ))
                denied_count += walk_stats.get("denied", 0)
            except (ValueError, RuntimeError) as exc:
                # Degrade loudly, never silently, and never into a prune.
                sys.stderr.write(
                    f"{RED}commit layer {layer} unavailable:{RESET} {exc}\n"
                    f"{YELLOW}Its existing rows are kept, not pruned.{RESET}\n"
                )
                degraded.add(layer)
                claimed.update(
                    p for p, in conn.execute(
                        "SELECT DISTINCT path FROM notes WHERE layer=?", (layer,)
                    )
                )
                continue
            for c in commits:
                rel = c["path"]
                if rel in claimed:
                    continue
                claimed.add(rel)
                # A commit is immutable: its sha fixes its message forever. So
                # "already stored" IS "up to date" -- no mtime compare needed,
                # and no `meta` high-water sha to keep in sync with the store.
                if not force and rel in existing_by_path:
                    skipped_uptodate += 1
                    continue
                changed_paths.add(rel)
                info = {
                    "title": c["title"], "ntype": c["ntype"], "raw_body": c["body"],
                    "embed_text": c["embed_text"], "created": "", "updated": "",
                    "confidence": "", "status": "", "access_count": 0,
                    "last_accessed": "",
                }
                oversized += add_pending(
                    pending, c["id"], rel, layer, c["mtime"], info,
                    [c["embed_text"]], chunk_cfg)
            continue

        layer_denies = layer_deny_prefixes(
            layer_cfg, deny_prefixes,
            store_prefixes=runtime_store_prefixes(root) if tree == "data" else ())
        if not layer_cfg.get("glob"):
            # A registry-named layer none of whose names resolved; said on
            # stderr where it was resolved. Its rows are kept, not pruned.
            degraded.add(layer)
            claimed.update(
                p for p, in conn.execute(
                    "SELECT DISTINCT path FROM notes WHERE layer=?", (layer,)
                )
            )
            continue
        for sub in expand_brace_glob(layer_cfg["glob"]):
            for fpath in sorted(root.glob(sub)):
                if not fpath.is_file():
                    continue
                rel = fpath.relative_to(root).as_posix()
                if is_denied(rel, layer_denies, file_denies):
                    denied_count += 1
                    continue            # NEVER read denied content
                if rel in claimed:
                    continue            # first layer to claim keeps the label
                claimed.add(rel)
                # The stat opens the same walk-then-read window the read below
                # closes, and raises the same FileNotFoundError, so it gets the
                # same answer. SKIPPING is correct here and nowhere near as
                # obvious as it looks, so the whole chain is written down:
                # the file is genuinely gone, this is an index for RETRIEVAL,
                # and a note that no longer exists must not be retrievable. The
                # bookkeeping already does the right thing with it -- `rel` is
                # in `changed_paths`, whose DELETE at the end of this function
                # drops the stale rows, and it is in `claimed`, so the prune
                # leaves it alone this pass and the next walk (which will not
                # list it at all) prunes it properly. Nothing downstream treats
                # a missing note as an error; `recall` answers from the rows
                # that are there.
                #
                # What skipping would otherwise cost is the claim, not the
                # index: the summary line below counts records "in scope" from
                # the walk, so a silent skip would report coverage this pass did
                # not have. Hence `vanished`, which that line reports and which
                # is printed by name.
                try:
                    mtime = fpath.stat().st_mtime
                except FileNotFoundError:
                    vanished.append(rel)
                    continue
                if (
                    not force
                    and rel in existing_by_path
                    and abs(existing_by_path[rel] - mtime) < 1e-6
                ):
                    skipped_uptodate += 1
                    continue
                changed_paths.add(rel)
                # Through the shared `read_sources`, which skips a vanished path
                # and WARNS naming it. `errors="replace"` is preserved: a byte
                # that does not decode is a real note, and the embedder wants its
                # text. Only the vanished case is skipped -- a permission error
                # or a directory here is a fault about a file that IS there and
                # still raises.
                sources = list(read_sources([fpath], errors="replace"))
                if not sources:
                    vanished.append(rel)
                    continue
                info = parse_note(sources[0][1])
                title = info["title"]
                if layer in chunk_layers:
                    pieces = chunk_text(
                        info["raw_body"],
                        max_chars=chunk_cfg["max_chars"],
                        overlap=chunk_cfg["overlap"],
                        max_chunks=chunk_cfg["max_chunks"],
                    )
                    # `chunk_text` truncates AT max_chunks, so `>=` was `==`: a
                    # file that filled the cap exactly, losing nothing, was
                    # logged as "hit chunk cap". Compare against what the text
                    # would have produced unbounded instead.
                    if _would_truncate(info["raw_body"], chunk_cfg):
                        capped += 1
                    embed_texts = [
                        f"{title}\n{pc}".strip() if title else pc.strip()
                        for pc in pieces
                    ]
                else:
                    embed_texts = [info["embed_text"]]  # title + snippet, as before
                oversized += add_pending(
                    pending, rel, rel, layer, mtime, info, embed_texts, chunk_cfg)

    # Prune paths no longer matched / now denied -> delete all their chunks (air-gap honest).
    #
    # Scoped to what this pass could see. A path is prunable when its layer was
    # WALKED here, or when its layer no longer exists in the config at all
    # (orphaned by a config edit, with no owner and no way back). A row belonging
    # to a layer this pass simply did not walk is left alone.
    #
    # Found the hard way on 2026-08-21: rebuilding one layer to A/B the commit
    # body variants deleted 122 skill and rule rows the pass never looked at. The
    # CLI could not reach it -- `cmd_build` always passes a store's full layer set
    # -- but a partial rebuild is a normal thing to do, and a store that silently
    # empties is the worst possible way to find out the call was wrong.
    configured = {lc["layer"] for lc in cfg["layers"]}
    prunable = {
        p for p, lyr in conn.execute("SELECT DISTINCT path, layer FROM notes")
        if lyr in layers or lyr not in configured
    }
    stale_paths = [p for p in existing_by_path if p not in claimed and p in prunable]
    for p in stale_paths:
        conn.execute("DELETE FROM notes WHERE path=?", (p,))
    # Delete old chunk rows of changed/new files so a now-smaller chunk count leaves no orphans.
    for p in changed_paths:
        conn.execute("DELETE FROM notes WHERE path=?", (p,))

    # `classification` is derived from `config/routing-map.yaml`, not from the
    # record, so an up-to-date row would keep the resolver's answer of the day
    # it was embedded (798 stale rows MEASURED 2026-09-13).
    #
    # So: one pass over the rows this build did NOT rewrite, asking the resolver
    # again with the tree the build already knows for the row's layer. Only the
    # column moves; the vector, mtime and access bookkeeping stay. The rows in
    # `changed_paths` are already deleted here and come back through the upsert
    # with a fresh answer, so nothing is asked twice. A degraded layer is
    # skipped: this pass cannot speak for a layer it could not walk. A layer with
    # no tree recorded was not walked by this pass either (partial rebuild) and
    # is left alone for the same reason the prune leaves it alone.
    refreshed = _refresh_classifications(conn, tree_by_layer, degraded, tree)
    _refresh_written_by(conn, root)

    # "records", not "files". `claimed` holds a file path for a glob layer, a
    # `<repo>@<sha>` for a commit layer, and a path carrying a line range for a
    # symbol layer. Only one of the three is a file.
    #
    # `vanished` is on this line for the reason `.claude/rules/scope-claims.md`
    # gives: "records in scope" comes from the walk, so without it a pass whose
    # corpus shrank underneath it prints exactly like a pass that read
    # everything. The names go to stderr beside it, because a count alone does
    # not tell the operator which note is missing from the index.
    print(
        f"{CYAN}build:{RESET} {len(claimed)} records in scope "
        f"({skipped_uptodate} up-to-date, {len(changed_paths)} changed -> "
        f"{len(pending)} chunks to embed, {denied_count} denied, "
        f"{len(stale_paths)} records pruned, {refreshed} classifications refreshed"
        + (f", {capped} hit chunk cap" if capped else "")
        + (f", {oversized} split to fit the embed window" if oversized else "")
        + (f", {len(vanished)} listed but gone before the read" if vanished else "")
        + ")"
    )
    if vanished:
        sys.stderr.write(
            f"{YELLOW}{len(vanished)} file(s) vanished between the walk and the "
            f"read and are NOT indexed:{RESET} " + ", ".join(vanished) + "\n"
        )

    if pending:
        # Embed and commit per FILE. A file's chunks are contiguous in `pending`
        # (built file-by-file above), so committing at file boundaries keeps the
        # index resumable: on this CPU a full build embeds ~0.7s/chunk (thousands
        # of chunks -> tens of minutes), and a single commit-at-end means any
        # interruption loses everything and the store reads as 0 notes mid-build
        # (looks hung). Per-file commit makes the store grow live, prints progress,
        # and lets an interrupted run resume -- the mtime/delete bookkeeping above
        # treats a not-yet-committed file as new (its rows were deleted, its mtime
        # absent on re-read) and re-embeds only what's missing.
        total_chunks = len(pending)
        done_chunks = 0
        done_files = 0
        for rel, grp in groupby(pending, key=lambda p: p[1]):
            items = list(grp)
            try:
                vectors = embed(
                    [it[6] for it in items],
                    model=cfg["model"],
                    host=cfg["host"],
                    headers=headers,
                )
            except EmbeddingError as e:
                conn.commit()  # preserve files already embedded this run
                # `resync_fts` promises "the two channels can never drift", and
                # this early return skipped it. Prune and changed-file DELETEs
                # are committed at file boundaries during the loop, so a failure
                # here left `notes` short of rows `notes_fts` still held, and the
                # lexical channel stayed stale until the next SUCCESSFUL build.
                try:
                    resync_fts(conn)
                    conn.commit()
                except sqlite3.Error as sync_exc:
                    sys.stderr.write(
                        f"{YELLOW}FTS resync after the failure also failed "
                        f"({sync_exc}); the lexical channel may be stale until the "
                        f"next successful build.{RESET}\n")
                sys.stderr.write(f"{RED}Embedding failed:{RESET} {e}\n")
                sys.stderr.write(
                    f"{YELLOW}Committed {done_files} files "
                    f"({done_chunks}/{total_chunks} chunks) before failure; "
                    f"re-run to resume.{RESET}\n"
                )
                conn.close()
                return 1
            for (cid, r, layer, idx, mtime, info, etext), vec in zip(items, vectors):
                # The tree is half of the classification question: `r` is
                # relative to the store's root, and without its tree a data-root
                # path could resolve as engine (operator ruling 2026-09-13: no
                # file of the Data Repository may be open). Closed label on a
                # routing refusal, as in the refresh above.
                try:
                    visibility = get_classification(r, tree=tree_by_layer.get(layer, tree))
                except RoutingRefused as exc:
                    _say_routing_refusal(exc)
                    visibility = "ceo-only"
                upsert_note(
                    conn, id_=cid, path=r, title=info["title"], layer=layer,
                    ntype=info["ntype"], mtime=mtime, body=etext, vec=vec,
                    created=info["created"], updated=info["updated"],
                    confidence=info["confidence"], status=info["status"],
                    classification=visibility,
                    chunk=idx,
                    access_count=info["access_count"], last_accessed=info["last_accessed"],
                    written_by=info.get("written_by", ""),
                )
            conn.commit()  # file boundary: durable + resumable
            done_chunks += len(items)
            done_files += 1
            n = len(items)
            suffix = f" {GRAY}({n} chunks){RESET}" if n > 1 else ""
            pct = done_chunks * 100 // total_chunks
            print(
                f"  {GREEN}+{RESET} {GRAY}{rel}{RESET}{suffix} "
                f"{CYAN}[{pct}%]{RESET}",
                flush=True,
            )

    # Derive the lexical channel from the settled notes table (handles the
    # incremental, prune, and pre-hybrid-migration cases in one cheap pass).
    resync_fts(conn)

    # Provenance is stamped only by a build that can honestly speak for the WHOLE
    # store. `record_provenance` used to run here unconditionally, which is what
    # erased the drift the warning at the top of this function had just printed:
    # see `record_mixed_provenance`.
    #
    # `whole_store` is the honest test for "every row in this store was written
    # by this pass". `force` alone is not: a build restricted to a subset of the
    # store's layers re-embeds only those, and rows in a layer this pass never
    # walked keep their old vectors.
    #
    # Layer NAMES alone are not it either. A layer whose source raised takes the
    # degrade-and-keep path above: its rows survive with the vectors they already
    # had, and its name is still in `layers`. So `build --force` against a
    # CodeGraph index that has been deleted, or a git repo the walk cannot read,
    # used to CLEAR the mixed-provenance flag and stamp the store as freshly
    # built by this model, while a whole layer held vectors from whichever model
    # wrote them last. The stamp is the only thing that would have said so.
    stored_layers = {lyr for (lyr,) in conn.execute("SELECT DISTINCT layer FROM notes")}
    whole_store = stored_layers <= set(layers) and not degraded
    #
    # An incremental pass reaching this line ran on the store's own embedder,
    # because `embedder_change` refused any other at the top; restamping it only
    # moves a gateway address. A `--force` pass that could not speak for the
    # whole store is the one route left to a second embedder's rows.
    partial_change = (force and not whole_store and embedder_change(
        conn, model=cfg["model"], host=cfg["host"], digest=digest, provider=provider,
        pin=pin))
    if force and whole_store:
        clear_mixed_provenance(conn)
        record_provenance(conn, model=cfg["model"], host=cfg["host"], digest=digest,
                          provider=provider, pin=pin)
    elif partial_change:
        if pending:
            record_mixed_provenance(
                conn, model=cfg["model"], host=cfg["host"], digest=digest,
                provider=provider)
            sys.stderr.write(
                f"{YELLOW}Flagged {root / store_rel} as mixed provenance; every "
                f"BUILD reports it until `build --force`. `stats` and `query` do "
                f"not read this flag.{RESET}\n"
            )
        # Nothing embedded: the store still holds exactly the vectors it held, so
        # the old stamp is still true and must stay, or the drift stops being
        # reportable on the next run.
    else:
        record_provenance(conn, model=cfg["model"], host=cfg["host"], digest=digest,
                          provider=provider, pin=pin)
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    conn.close()
    print(f"{BOLD}Index ready:{RESET} {total} notes -> {root / store_rel}")
    return 0


# ============================================================
# Query
# ============================================================

def _load_index(conn):
    """Return (ids, metas_by_id, normalized matrix) for the whole index.

    ids align row-for-row with the matrix; metas_by_id maps id -> meta dict.
    Layer filtering is applied later in Python so the dense and sparse channels
    share one consistent universe of ids.
    """
    rows = conn.execute(
        "SELECT id, path, title, layer, ntype, dim, mtime, "
        "created, updated, confidence, status, classification, chunk, "
        "access_count, last_accessed, written_by, embedding FROM notes"
    ).fetchall()
    if not rows:
        return [], {}, None
    ids, metas, vecs = [], {}, []
    for (id_, path, title, lyr, ntype, dim, mtime, created, updated, confidence,
         status, classification, chunk, access_count, last_accessed, written_by,
         blob) in rows:
        ids.append(id_)
        metas[id_] = {
            "path": path, "title": title, "layer": lyr, "ntype": ntype,
            "mtime": mtime, "created": created, "updated": updated,
            "confidence": confidence, "status": status,
            "classification": classification or "", "chunk": chunk or 0,
            "access_count": access_count or 0, "last_accessed": last_accessed or "",
            "written_by": written_by or "",
        }
        vecs.append(np.frombuffer(blob, dtype=np.float32, count=dim))
    matrix = np.vstack(vecs).astype(np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return ids, metas, matrix / norms


def _fts_match_expr(text: str):
    """Build a safe FTS5 MATCH string: OR over double-quoted word tokens.

    Quoting each token as a string literal neutralises FTS5 operators, so an
    arbitrary RU/EN query can never become a syntax error. Returns None when the
    query has no usable tokens (sparse channel then contributes nothing).
    """
    seen, toks = set(), []
    for raw in TOKEN_RE.findall(text.lower()):
        if len(raw) >= 2 and raw not in seen:
            seen.add(raw)
            toks.append(raw)
    if not toks:
        return None
    return " OR ".join(f'"{t}"' for t in toks)


def _sparse_ids(conn, match_expr, limit):
    """Return note ids matched by BM25, best-first. Empty if no match expr."""
    if not match_expr:
        return []
    rows = conn.execute(
        "SELECT id FROM notes_fts WHERE notes_fts MATCH ? "
        "ORDER BY bm25(notes_fts) LIMIT ?",
        (match_expr, limit),
    ).fetchall()
    return [r[0] for r in rows]


def _path_match_ids(text, ids, cos_by_id, in_layer_fn, df_cap=PATH_TOKEN_DF_CAP):
    """Ids whose humanized PATH contains a RARE query token, best-cosine first.

    A folder/project/client name (e.g. 'Meridian') is a high-precision identifier:
    these matches are admitted to the candidate pool without the convergence gate,
    so a pure path-name query surfaces the file even when its body is semantically
    unrelated to the bare name. The rarity cap (document-frequency <= df_cap) keeps
    generic path words (outputs, datastore, a year) -- which match many files --
    OUT of this channel; those remain subject to the normal gated sparse channel.
    """
    toks = {t for t in TOKEN_RE.findall(text.lower()) if len(t) >= 2}
    if not toks:
        return []
    by_tok: dict = {}
    docs_by_tok: dict = {}
    for id_ in ids:
        # `ids` is one row per CHUNK (`path#N`), and `humanize_path` strips the
        # suffix -- so counting rows counted a 12-chunk file twelve times and a
        # genuinely rare project name could cross df_cap on two long notes,
        # denying the bypass to exactly the corpus chunking exists for. The
        # docstring says document-frequency; this now measures it.
        doc = id_.rsplit("#", 1)[0]
        ptoks = set(humanize_path(id_).lower().split())
        for t in toks & ptoks:
            by_tok.setdefault(t, []).append(id_)
            docs_by_tok.setdefault(t, set()).add(doc)
    admitted = set()
    for t, lst in by_tok.items():
        if len(docs_by_tok[t]) <= df_cap:   # rare token -> specific identifier
            admitted.update(i for i in lst if in_layer_fn(i))
    return sorted(admitted, key=lambda i: cos_by_id.get(i, 0.0), reverse=True)


def _rrf_fuse(dense_ids, sparse_ids, k=RRF_K):
    """Reciprocal Rank Fusion of two ranked id lists -> (ranked_ids, scores)."""
    scores = {}
    for ranked in (dense_ids, sparse_ids):
        for rank, id_ in enumerate(ranked, start=1):
            scores[id_] = scores.get(id_, 0.0) + 1.0 / (k + rank)
    ranked = sorted(scores, key=lambda i: scores[i], reverse=True)
    return ranked, scores


# ============================================================
# R7: recency x importance x relevance combiner
# ============================================================

_CONFIDENCE_SCORE = {"high": 1.0, "medium": 0.7, "med": 0.7, "low": 0.4}
_EVERGREEN_FLOOR = 0.7  # neutral recency for timeless / unknown-age notes
# Gap #2: a high-confidence fact decays slower (longer effective half-life);
# a low-confidence one decays faster. First-cut defaults -- tune after
# observing real dream-shadow reports and /recall ranking behaviour.
CONF_HALFLIFE_MULT = {"high": 1.5, "medium": 1.0, "med": 1.0, "low": 0.6}


def _date_to_ts(s):
    """Parse an ISO date / datetime string to a UTC epoch, or None."""
    if not s:
        return None
    s = str(s).strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _recency_score(meta, halflife_days, now_ts):
    """Exponential recency in (0, 1]. ``status: evergreen`` floors at ~0.7 so
    timeless positions/sources are not buried by age (e.g. the 2026-03-18
    valuation-path position). Falls back to mtime when no created/updated date
    is present; unknown age stays neutral rather than penalised.

    Gap #2: a `last_accessed` date more recent than `updated`/`created`/`mtime`
    resets the effective age to the citation date (a fact /recall keeps citing
    stays "fresh" even if its content hasn't been edited), and the half-life
    itself scales with confidence (CONF_HALFLIFE_MULT) so a high-confidence
    fact decays slower than a low-confidence one at the same age."""
    if (meta.get("status") or "").strip().lower() == "evergreen":
        return _EVERGREEN_FLOOR
    ts = _date_to_ts(meta.get("updated")) or _date_to_ts(meta.get("created"))
    if ts is None:
        ts = meta.get("mtime")
    access_ts = _date_to_ts(meta.get("last_accessed"))
    if access_ts is not None and (ts is None or access_ts > ts):
        ts = access_ts
    if not ts:
        return _EVERGREEN_FLOOR
    age_days = max(0.0, (now_ts - ts) / 86400.0)
    halflife = halflife_days if halflife_days and halflife_days > 0 else 180
    conf_mult = CONF_HALFLIFE_MULT.get((meta.get("confidence") or "").strip().lower(), 1.0)
    effective_halflife = halflife * conf_mult
    return float(0.5 ** (age_days / effective_halflife))


# Writers whose records rank below a person's (HOS-192). `memory-distil` and,
# before HOS-198, `cognee-distil` are the values the retired record writer
# stamped (it was deleted on 2026-10-16); records on disk still carry both.
MACHINE_WRITERS = frozenset({"memory-distil", "cognee-distil"})
MACHINE_WRITTEN_FACTOR = 0.8


def _importance_score(meta):
    """Importance from confidence (high/medium/low -> 1.0/0.7/0.4); episodes
    biased down (event logs are less load-bearing for advice). Default medium.

    Gap #2: scaled by a reinforcement bonus from `access_count` -- a fact
    /recall keeps citing ranks above an otherwise-identical, never-cited one."""
    base = _CONFIDENCE_SCORE.get((meta.get("confidence") or "").strip().lower(), 0.7)
    if (meta.get("ntype") or "").strip().lower() == "episode":
        base *= 0.8
    # HOS-192: a record the distiller wrote yields to one a person wrote. The
    # operator's decision 2026-10-03, "With a mark" (translated): it keeps writing, marked,
    # and gives way to his direct words. The factor is the episode bias above,
    # reused rather than tuned: no measurement chose it.
    if (meta.get("written_by") or "").strip() in MACHINE_WRITERS:
        base *= MACHINE_WRITTEN_FACTOR
    return base * salience.reinforcement_bonus(int(meta.get("access_count") or 0))


def _combined(cos, recency, importance, weights):
    """Weighted sum of semantic relevance, recency, and importance."""
    return (weights["semantic"] * cos
            + weights["recency"] * recency
            + weights["importance"] * importance)


def _best_in_scope(ids, cos_by_id, in_layer_fn):
    """The best cosine among rows the query could actually have returned."""
    in_scope = [cos_by_id[i] for i in ids if in_layer_fn(i)]
    return float(max(in_scope)) if in_scope else None


def _query_store(conn, qvec, full_text, *, threshold, layer, allowed, near_miss_margin=0.0):
    """Full hybrid retrieval (dense + BM25 + path-token) for ONE store.

    Returns per-store results carrying raw signals so the caller can pool
    candidates across stores and run a SINGLE RRF + R7 fusion. The BM25 and
    path channels are FTS-bound to this store's connection, so they must run
    here (per store) -- not over a concatenated matrix. ``best`` is the store's
    max cosine (None for an unbuilt/empty store). Dense uses the truncated query
    vector; sparse/path use ``full_text`` -- exactly as the pre-split path did."""
    ids, metas, matrix = _load_index(conn)
    if matrix is None:
        return {"dense_ids": [], "sparse_ids": [], "path_ids": [], "near_ids": [],
                "cos_by_id": {}, "metas": {}, "chunks_total": {}, "best": None}
    q = np.asarray(qvec, dtype=np.float32)
    qn = np.linalg.norm(q) or 1.0
    scores = matrix @ (q / qn)
    cos_by_id = {ids[i]: float(scores[i]) for i in range(len(ids))}

    # Chunk -> file collapse: a file is represented by its BEST-scoring chunk, so a
    # multi-chunk document yields ONE hit, not N fragments. `_collapse` maps any
    # chunk id to its file's best chunk and dedups, order-preserving.
    best_chunk_by_path, chunks_total = {}, {}
    for _id in ids:
        _p = metas[_id]["path"]
        chunks_total[_p] = chunks_total.get(_p, 0) + 1
        if _p not in best_chunk_by_path or cos_by_id[_id] > cos_by_id[best_chunk_by_path[_p]]:
            best_chunk_by_path[_p] = _id

    def _collapse(id_list):
        out, seen = [], set()
        for i in id_list:
            b = best_chunk_by_path[metas[i]["path"]]
            if b not in seen:
                seen.add(b)
                out.append(b)
        return out

    def in_layer(id_):
        if layer:
            return metas[id_]["layer"] == layer
        return allowed is None or metas[id_]["layer"] in allowed

    # Dense channel: cosine gated by the threshold (the honest-gap anchor, untouched).
    dense_ids = [
        ids[i]
        for i in np.argsort(scores)[::-1]
        if cos_by_id[ids[i]] >= threshold and in_layer(ids[i])
    ][:RANK_CAP]

    # Near-miss channel: cosine in [threshold - near_miss_margin, threshold).
    # NOT part of the normal result set -- the caller uses it ONLY when every
    # other channel came back empty, in place of declaring a gap. Kept separate
    # from `dense_ids` on purpose: a below-threshold hit must never silently
    # join a confident result set.
    near_ids = []
    if near_miss_margin > 0:
        floor = threshold - near_miss_margin
        near_ids = [
            ids[i]
            for i in np.argsort(scores)[::-1]
            if floor <= cos_by_id[ids[i]] < threshold and in_layer(ids[i])
        ][:RANK_CAP]

    # Sparse channel: BM25 lexical match, gated by convergence with the dense
    # signal so a generic-term match on an unrelated note cannot leak through.
    sparse_floor = threshold - SPARSE_COS_MARGIN
    sparse_ids = [
        i for i in _sparse_ids(conn, _fts_match_expr(full_text), SPARSE_LIMIT)
        if i in metas and in_layer(i) and cos_by_id[i] >= sparse_floor
    ]

    # Path-match channel: rare query tokens that match a file's PATH (folder /
    # project / client name). Admitted WITHOUT the convergence gate -- a path name
    # is a precise identifier, not generic-content noise.
    path_ids = _path_match_ids(full_text, ids, cos_by_id, in_layer)

    return {
        "dense_ids": _collapse(dense_ids),
        "sparse_ids": _collapse(sparse_ids),
        "path_ids": _collapse(path_ids),
        "near_ids": _collapse(near_ids),
        "cos_by_id": cos_by_id,
        "metas": metas,
        "chunks_total": chunks_total,
        # SCOPED. `scores.max()` over the whole matrix reported a best from a
        # layer the query was scoped away from, so `--layer odin` could print
        # "Nothing above threshold 0.55 (best 0.62)" -- a number contradicting
        # the message, from a row that was never a candidate.
        "best": _best_in_scope(ids, cos_by_id, in_layer),
    }


def _mirror_access_counts(cfg, counts: dict, today: str) -> None:
    """Write the freshly bumped counts into the `memory` layer's store.

    The bump preserves the source file's mtime (see memory_touch.touch_file),
    which is what keeps the reconcile hook, dream-shadow and memory_health
    honest — but it also means the nightly incremental build skips the file and
    would never notice the new count. So the ranking signal is mirrored here,
    in the module that owns the store, rather than a day later or not at all.
    No re-embedding: one column on rows that already exist.

    Best-effort like the bump itself. This is on the read path; a store that
    cannot be opened must cost the recall nothing.
    """
    if not counts:
        return
    try:
        root, store_rel = _layer_store_map(cfg)["memory"]
    except Exception as exc:  # noqa: BLE001 - boundary; recall must still answer
        sys.stderr.write(f"touch: no store routes the memory layer ({exc})\n")
        return
    if not (root / store_rel).is_file():
        return          # nothing built yet; the next build reads the files
    try:
        conn = open_store(root, store_rel)
        try:
            conn.executemany(
                "UPDATE notes SET access_count = ?, last_accessed = ? WHERE path = ?",
                [(n, today, p) for p, n in counts.items()],
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        sys.stderr.write(f"touch: index update failed ({exc})\n")


def _touch_memory_hits(hits, cfg=None) -> int:
    """Reinforce auto-memory files returned as a CONFIDENT hit. Returns the
    number of files actually bumped.

    Ranking signal only. Nothing downstream of this counter may read a low
    value as grounds for removal — see the standing directive that auto-memory
    is never pruned, only deprioritized.

    Best-effort by construction: this runs on the read path, inside a hook with
    a hard timeout, so a bump that cannot be written must cost the recall
    nothing. Every failure is reported on stderr (the hook logs it) and then
    swallowed per file.

    When `cfg` is supplied the new counts are mirrored into the index in the
    same call, so the signal ranks the very next query.
    """
    try:
        from scripts.utils.memory_touch import TouchError, touch_if_stale  # noqa: F401
        from scripts.utils import workspace as _ws
    except Exception as exc:  # noqa: BLE001 - boundary; recall must still answer
        sys.stderr.write(f"touch: unavailable ({exc})\n")
        return 0
    try:
        mem_dir = _ws.get_auto_memory_dir()
        today = datetime.now(_ws.get_default_tz()).date().isoformat()
    except Exception as exc:  # noqa: BLE001 - boundary; recall must still answer
        sys.stderr.write(f"touch: cannot resolve auto-memory dir ({exc})\n")
        return 0

    counts: dict[str, int] = {}
    seen = dict.fromkeys(h["path"] for h in hits if h.get("layer") == "memory")
    for path in seen:
        try:
            new_count = touch_if_stale(path, mem_dir, today)
        except Exception as exc:  # noqa: BLE001 - see below; logged, never raised
            # Total per file, as the docstring already promised. TouchError and
            # OSError were named explicitly, which left UnicodeDecodeError (a
            # ValueError, from read_text on an undecodable memory file) to
            # escape cmd_query, exit non-zero, and take recall down silently on
            # EVERY prompt until someone read stderr. A bump is a ranking
            # nicety; nothing it can hit is worth failing a recall over.
            sys.stderr.write(f"touch: skipped {path} ({exc})\n")
            continue
        if new_count is not None:
            counts[path] = new_count
    if cfg is not None:
        _mirror_access_counts(cfg, counts, today)
    return len(counts)


def _should_touch(args, near_miss: bool) -> bool:
    """Whether this result set may be counted as a use of memory.

    Two rules, both load-bearing. Opt-in: without --touch the read path writes
    nothing, so /recall, the CLI and the tests are unaffected. Confident only:
    a near-miss block is presented as "relevance NOT established", and counting
    it would train the ranking on noise.
    """
    return bool(getattr(args, "touch", False)) and not near_miss


# The calibration directory is named once, in `scripts/utils/calibration_store.py`,
# which the writer (`scripts/recall-calibration.py`) reads too: a reader and a
# writer that resolve differently fail here as `return None`, "nothing
# calibrated yet".
# A calibrated cut outside this range is not a calibration, it is a corrupt or
# mis-scaled file. Cosine over a normalised embedding lives in [0, 1], and a cut
# at either end turns recall into "everything is confident" or "nothing is".
CALIBRATION_FLOOR, CALIBRATION_CEILING = 0.20, 0.90


def calibrated_threshold():
    """The cut a calibration run recorded, or None.

    It lives in the DATA overlay rather than in `config/memory-index.yaml`, and
    that placement is the whole design:

    * a weekly job writing into a TRACKED engine file dirties the working tree,
      trips the push gates, and puts a machine-derived number into a public
      repository;
    * a gitignored ENGINE file would be invisible in every worktree, which is the
      defect fixed on 2026-09-06 for the embedder pin -- repeating it here would
      mean the calibrated cut applied in HELM and silently did not in any yard;
    * the threshold is a function of the CORPUS, and the corpus is the overlay.
      Every yard reaches the overlay through the data-root helpers by design.

    A public clone with no overlay simply gets None and the shipped value, which
    is the same behaviour it has today.

    Fails safe in the quiet direction: anything unreadable, unparseable or out of
    range returns None, so recall falls back to the number this repository ships
    rather than to a value nobody can account for.

    `DataPathError` is in that list (2026-09-24) because the registry name
    `memory-calibration` can refuse: a registry that will not parse, or one
    that omits the name. It is a `RuntimeError`, so without it a refusal would
    leave as a traceback from a function designed to fail quiet. Caught by its
    BASE class so a new subclass is covered too.
    """
    try:
        from scripts.utils.calibration_store import current_path
        path = current_path()
        with open(path, encoding="utf-8") as fh:
            value = float((json.load(fh) or {}).get("threshold"))
    except (OSError, UnicodeDecodeError, ValueError, TypeError,
            json.JSONDecodeError, DataPathError):
        return None
    if not CALIBRATION_FLOOR <= value <= CALIBRATION_CEILING:
        sys.stderr.write(
            f"memory-index: ignoring calibrated threshold {value} outside "
            f"[{CALIBRATION_FLOOR}, {CALIBRATION_CEILING}]\n")
        return None
    return value


def resolve_threshold(cfg, cli_threshold, allowed):
    """The confidence cut for one query: CLI value, else per-layer, else global.

    Cosine is not comparable across registers. Measured 2026-08-21 on the frozen
    25-query commit set: the correct answer to a paraphrased or Russian question
    scores 0.456-0.597, while the SAME index answers a keyword query at
    0.590-0.697. One global cut calibrated on prose therefore reports a gap on a
    commit question it did in fact rank first -- 77% against the agreed 80% bar,
    where 0.45 gives 85%. The cost is measured too: one nonsense query in six
    gets a confident-looking hit at 0.45, none at 0.55. Scoped to the layers that
    need it, so `content` keeps 0.55 and its honest-gap behaviour.

    `allowed` is the resolved layer set, or None for "no restriction". The
    per-layer cut applies only when the query resolves to exactly ONE layer (via
    `--layer` or a single-layer collection): with several layers in play there is
    no single right cut, so the global one stands. An explicit `--threshold`
    always wins.

    A function rather than eight lines inside `cmd_query` (2026-09-01); the
    per-layer cut is verified by running a query.
    """
    if cli_threshold is not None:
        return cli_threshold
    # The calibrated global cut, when one exists, stands in for the shipped
    # global cut and for nothing else. A layer that carries its OWN measured cut
    # (the commit layers, 0.45 on a 25-query set) still wins below: that number
    # was measured on a different register and a corpus-wide calibration of the
    # prose cut says nothing about it.
    threshold = cfg.get("calibrated_threshold")
    if threshold is None:
        threshold = cfg["threshold"]
    if allowed and len(allowed) == 1:
        only = next(iter(allowed))
        per_layer = next(
            (lc.get("threshold") for lc in cfg["layers"]
             if lc["layer"] == only and lc.get("threshold") is not None),
            None,
        )
        if per_layer is not None:
            threshold = float(per_layer)
    return threshold


def cmd_query(args) -> int:
    _t0 = time.perf_counter()

    def _emit(gap, hits_list):
        try:
            from scripts.utils.memory_ops_log import log_recall
            log_recall(
                query_snippet=getattr(args, "text", ""),
                collection=getattr(args, "collection", "content"),
                layer=getattr(args, "layer", None),
                top_score=(hits_list[0]["score"] if hits_list else None),
                gap=bool(gap),
                n_hits=len(hits_list),
                threshold=getattr(args, "threshold", None),
                latency_ms=round((time.perf_counter() - _t0) * 1000, 1),
                hit_paths=[h.get("path") for h in hits_list],
            )
        except Exception as exc:  # noqa: BLE001 - recall must answer regardless
            # Reported, THEN swallowed -- this file's standard everywhere else.
            # A silent return meant a broken ops log was noticed only when
            # somebody went to read it and found nothing.
            sys.stderr.write(f"{YELLOW}recall ops-log write failed:{RESET} {exc}\n")
            return

    cfg = load_config(get_workspace_root())  # memory-index.yaml is engine config

    # Nothing below can run without an embedder, and a silent empty result is
    # indistinguishable from "memory has nothing on this" — the exact confusion
    # the 2026-08-23 directive exists to end. Refuse, in both renderings.
    if cfg.get("host") is None:
        reason = cfg.get("host_error") or "the pinned embedder is unreachable"
        if getattr(args, "json", False):
            print(json.dumps(embed_unavailable_payload(reason, cfg.get("host_remedy")),
                             ensure_ascii=False))
        else:
            sys.stderr.write(
                f"{RED}Cannot search memory: {reason}{RESET}\n"
            )
        return 3

    near_miss_margin = float(cfg.get("near_miss_margin", 0.12) or 0.0)
    top_k = args.top_k or cfg["top_k"]
    layer = args.layer
    qtext = args.text[:SNIPPET_CHARS]  # symmetry with build-time snippet window

    # Resolve the allowed layer set AND which physical store(s) to open. --layer
    # wins (one layer; preserves /odin's `--layer odin` contract). Else the
    # collection's layers; `all` lifts the restriction. content -> data store
    # only (the /recall default, unchanged); code -> engine code store; all ->
    # both, pooled. A missing collections map falls back to no-restriction over
    # all stores (back-compat). getattr defaults keep bare-namespace callers working.
    collection = getattr(args, "collection", "content")
    want_json = getattr(args, "json", False)
    coll_map = cfg.get("collections") or {}
    targets = _store_targets(cfg)
    if layer:
        allowed = {layer}
        lmap = _layer_store_map(cfg)
        if layer not in lmap:
            # Routing to zero stores made a typo'd layer read as an empty index,
            # so the operator was told to rebuild something that was fine.
            # Unknown COLLECTIONS already get a named error; layers did not.
            sys.stderr.write(
                f"{RED}Unknown layer:{RESET} {layer!r}. "
                f"Configured layers: {', '.join(sorted(lmap)) or '(none)'}\n")
            return 2
        stores = [lmap[layer]]
    elif collection == "all":
        allowed = None
        stores = [(t["root"], t["store_rel"]) for t in targets]
    elif collection in coll_map:
        allowed = set(coll_map[collection])
        stores = [(t["root"], t["store_rel"]) for t in targets if t["layers"] & allowed]
    elif collection == "content" and not coll_map:
        allowed = None
        stores = [(t["root"], t["store_rel"]) for t in targets]
    else:
        known = ", ".join(sorted(coll_map)) or "(none configured)"
        sys.stderr.write(
            f"{RED}Unknown collection '{collection}'.{RESET} "
            f"Known: {known}, or 'all'.\n"
        )
        return 1

    # Per-layer threshold; the reasoning and the measurements live on
    # `resolve_threshold`, which is what the tests call.
    threshold = resolve_threshold(cfg, args.threshold, allowed)

    try:
        qvec = embed([qtext], model=cfg["model"], host=cfg["host"])[0]
    except EmbeddingError as e:
        # `--json` must emit JSON on EVERY exit -- the same contract the
        # empty-index branch below states, and the same consumer:
        # `.claude/hooks/recall-inject.py` logs "unparseable JSON" and stays
        # silent. This path printed to stderr only, so an embed failure AFTER a
        # successful host resolution (model not pulled on the pinned host, or
        # ollama killed between resolve and embed) left stdout empty and the hook
        # degraded blind on every prompt until the host recovered. That is
        # precisely the case `embed_unavailable_payload` was written for, and the
        # host-down branch above already routes through it.
        if want_json:
            print(json.dumps(embed_unavailable_payload(str(e), cfg.get("host_remedy")),
                             ensure_ascii=False))
        else:
            sys.stderr.write(f"{RED}Embedding failed:{RESET} {e}\n")
        return 1

    # Per-store hybrid retrieval, then pool. Each store runs its own dense + BM25
    # + path channels (BM25/path are FTS-bound per store); we pool the candidates
    # and run ONE RRF + R7 fusion below. For a single store the pooled lists equal
    # that store's lists in order -> byte-identical to the pre-split path (the
    # /recall content-regression guard).
    cos_by_id, metas, chunks_total = {}, {}, {}
    all_dense, all_sparse, all_path, all_near = [], [], [], []
    best = None
    for s_root, s_rel in stores:
        # A read path must not materialize a store. `open_store` mkdirs the
        # parent and lets sqlite3.connect create the file, then runs the schema
        # DDL -- so a query against a never-built collection CREATED it, and
        # after that `stats` could no longer tell "never built" from "built and
        # empty". `_stats_one_store` and `_mirror_access_counts` both guard on
        # existence for this reason; this loop did not. An absent store
        # contributes zero candidates, which lands in the empty-index branch
        # below.
        if not (s_root / s_rel).is_file():
            continue
        conn = open_store(s_root, s_rel)
        res = _query_store(conn, qvec, args.text,
                           threshold=threshold, layer=layer, allowed=allowed,
                           near_miss_margin=near_miss_margin)
        conn.close()
        cos_by_id.update(res["cos_by_id"])
        metas.update(res["metas"])
        chunks_total.update(res["chunks_total"])
        all_dense.extend(res["dense_ids"])
        all_sparse.extend(res["sparse_ids"])
        all_path.extend(res["path_ids"])
        all_near.extend(res["near_ids"])
        if res["best"] is not None:
            best = res["best"] if best is None else max(best, res["best"])

    if not metas:
        # `--json` must emit JSON on EVERY exit, including this one. Until
        # 2026-08-21 this path printed prose regardless, so a machine caller got
        # unparseable output and had to guess: `.claude/hooks/recall-inject.py`
        # logs "unparseable JSON" and stays silent (degrades, but blind), and
        # `scripts/eval-query-set.py` died on a raw JSONDecodeError traceback.
        # An empty store is a normal state -- a fresh clone, or a layer whose
        # definition was withdrawn -- not a reason to break the contract.
        if want_json:
            print(json.dumps({"hits": [], "gap": True, "empty_index": True},
                             ensure_ascii=False))
        else:
            print(f"{YELLOW}Index is empty.{RESET} "
                  f"Run: python3 scripts/memory-index.py build")
        _emit(gap=True, hits_list=[])
        return 0

    # Pool: dense re-sorted globally by cosine (a no-op for a single store, which
    # already returns cosine-descending); sparse/path concatenated with each
    # store's internal order preserved. Single store -> byte-identical lists.
    dense_ids = sorted(all_dense, key=lambda i: cos_by_id.get(i, 0.0), reverse=True)
    sparse_ids = all_sparse
    path_ids = all_path
    combined_sparse = list(dict.fromkeys(path_ids + sparse_ids))

    # A PATH-ONLY match is a lead, never a confident answer.
    #
    # `_path_match_ids` is admitted without the convergence gate on purpose, so a
    # query naming a folder or a client finds the file even when its body is
    # semantically unrelated. That is the right candidate rule and is unchanged.
    # What was wrong is that its presence in `combined_sparse` also SUPPRESSED
    # this branch, so the payload went out with no `near_miss` key and every
    # consumer read it as confident.
    #
    # MEASURED 2026-09-06 at the shipped threshold, `who runs our beekeeping
    # operation` (nothing in the corpus is about beekeeping):
    #
    #     gap False   near_miss None   confident None
    #     0.3957  auto-memory/a-lighthouse-that-runs-on-a-different-clock-than-the-harbour.md  # leak-guard: ok (history: measured output)
    #     0.3963  auto-memory/a-dredger-that-runs-a-narrower-channel-than-the-chart.md  # leak-guard: ok (history: measured output)
    #     0.3775  auto-memory/a-ferry-that-runs-once-when-the-timetable-says-twice.md  # leak-guard: ok (history: measured output)
    #
    # All five returned rows shared exactly one token with the question: `runs`.
    # Across eight such controls, five came back confident this way. It lands in
    # `.claude/hooks/recall-inject.py`, which reads `near_miss` and nothing else,
    # so the block was headed "Memory relevant to this message" 726 times a month.
    #
    # `PATH_TOKEN_DF_CAP` cannot separate these: `runs` is under the cap. Nor can
    # the ordinary-English floor of `scripts/utils/content_denylist.py`, tried and
    # rejected here on 2026-09-06 -- `is_ordinary_english` returns True for
    # `meridian`, `syria` and `patagonia`, so it would delete the channel's own
    # worked example. It is a dictionary of the language, not a test for a name.
    #
    # So the fix is at the CONFIDENCE boundary rather than the candidate rule:
    # `sparse_ids` still confers confidence, because BM25 is floored at
    # `threshold - SPARSE_COS_MARGIN` and is genuine convergence; `path_ids`
    # alone does not. The file still surfaces, flagged, which is what a filename
    # match honestly is.
    near_miss = False
    if not dense_ids and not sparse_ids:
        near_ids = sorted(all_near, key=lambda i: cos_by_id.get(i, 0.0), reverse=True)
        if not near_ids and not path_ids:
            best_val = best if best is not None else 0.0
            if want_json:
                payload = {"hits": [], "gap": True, "best": round(best_val, 4),
                           "threshold": round(float(threshold), 4)}
                # A gap on the wrong embedder still has to say so: "nothing found"
                # and "found nothing because the pinned host was asleep" read the
                # same to the caller, and only one of them is a gap.
                fallback = host_fallback_info(cfg.get("host_preferred"), cfg["host"])
                if fallback:
                    payload["embed_fallback"] = fallback
                print(json.dumps(payload, ensure_ascii=False))
                _emit(gap=True, hits_list=[])
                return 0
            print(
                f"{YELLOW}Nothing above threshold {threshold:.2f}{RESET} "
                f"(best {best_val:.3f}, no lexical match) -- a gap in this area of memory."
            )
            _emit(gap=True, hits_list=[])
            return 0
        # Below the confidence threshold but inside the margin: answer, flagged.
        near_miss = True
        dense_ids = near_ids

    fused, _ = _rrf_fuse(dense_ids, combined_sparse)
    dense_set, sparse_set, path_set = set(dense_ids), set(sparse_ids), set(path_ids)

    # R7: re-rank the RRF candidate set by recency x importance x relevance.
    # RRF still generates the candidates (and the dense gate still guards the
    # honest-gap signal); this combiner only reorders what survived.
    weights = cfg["rank_weights"]
    halflife = cfg["recency_halflife_days"]
    now_ts = time.time()

    def _rank_score(id_):
        m = metas[id_]
        return _combined(
            cos_by_id[id_],
            _recency_score(m, halflife, now_ts),
            _importance_score(m),
            weights,
        )

    fused = sorted(fused, key=_rank_score, reverse=True)[:top_k]

    # Reverse layer -> collection map, so each hit carries its collection tag.
    coll_of = {}
    for cname, lyrs in coll_map.items():
        for lyr in lyrs:
            coll_of.setdefault(lyr, cname)

    hits = []
    for id_ in fused:
        m = metas[id_]
        channels = []
        if id_ in dense_set:
            channels.append("dense")
        if id_ in sparse_set:
            channels.append("bm25")
        if id_ in path_set:
            channels.append("path")
        hits.append({
            "path": m["path"],
            "title": m["title"] or m["path"],
            "layer": m["layer"],
            "ntype": m["ntype"] or "",
            "classification": m.get("classification") or "",
            "collection": coll_of.get(m["layer"], ""),
            "score": round(cos_by_id[id_], 4),
            "channels": channels,
            "chunk": m.get("chunk", 0),
            "chunks_total": chunks_total.get(m["path"], 1),
        })
        if near_miss:
            hits[-1]["below_threshold"] = True

    # Reinforcement is written only for a result the caller will present as an
    # answer. A near-miss block is explicitly "relevance NOT established", so
    # counting it would train the ranking on noise.
    if _should_touch(args, near_miss):
        _touch_memory_hits(hits, cfg)

    if want_json:
        payload = {"hits": hits, "gap": False}
        if near_miss:
            payload["near_miss"] = True
            payload["confident"] = False
        fallback = host_fallback_info(cfg.get("host_preferred"), cfg["host"])
        if fallback:
            payload["embed_fallback"] = fallback
        print(json.dumps(payload, ensure_ascii=False))
        # `gap=not near_miss`: the UI labels a near-miss "relevance NOT
        # established", and logging it as gap=False trained the recall-quality
        # dashboard to count it as a hit. The touch path already refuses to
        # learn from near-misses; the ops log now agrees with it.
        _emit(gap=near_miss, hits_list=hits)
        return 0

    if near_miss:
        print(
            f"{YELLOW}No confident match above {threshold:.2f}.{RESET} "
            f"{GRAY}Nearest material by similarity, relevance NOT established -- "
            f"treat as a lead, not an answer.{RESET}"
        )
    else:
        print(f"{BOLD}Associative recall{RESET} {GRAY}(recency x importance x relevance){RESET}")
    for h in hits:
        tag = "+".join(h["channels"])
        ntype = f" {GRAY}{h['ntype']}{RESET}" if h["ntype"] else ""
        cls = f" {GRAY}[{h['classification']}]{RESET}" if h["classification"] else ""
        chunk = (f" {GRAY}(chunk {h['chunk'] + 1}/{h['chunks_total']}){RESET}"
                 if h["chunks_total"] > 1 else "")
        print(
            f"  {GREEN}{h['score']:.3f}{RESET} {GRAY}{tag:10}{RESET} {CYAN}{h['layer']:10}{RESET}"
            f"{ntype}{cls}  {h['title']}{chunk}"
        )
        print(f"          {GRAY}{h['path']}{RESET}")
    _emit(gap=near_miss, hits_list=hits)
    return 0


# ============================================================
# Stats
# ============================================================

def cmd_stats(args) -> int:
    cfg = load_config(get_workspace_root())  # memory-index.yaml is engine config
    top_access = int(getattr(args, "top_access", 0) or 0)
    for i, t in enumerate(_store_targets(cfg)):
        if i:
            print()
        _stats_one_store(t["name"], t["root"], t["store_rel"], top_access)
    return 0


def top_access_rows(conn, limit: int) -> list:
    """Memory-layer entries ranked by access_count. A DIAGNOSTIC, not a worklist.

    Reads the reinforcement loop from the outside: if the same names hold the
    top of this list month after month, retrieval is reinforcing what retrieval
    already surfaces and REINFORCE_K is weighted too heavily. A LOW count here
    means a lower position in recall and nothing else — auto-memory is never
    pruned, so nothing at the bottom of this list is a candidate for anything.

    Returns [(path, access_count, last_accessed)], chunked files collapsed to
    one row (every chunk of a file carries the same count).
    """
    return conn.execute(
        "SELECT path, MAX(access_count), MAX(COALESCE(last_accessed, '')) "
        "FROM notes WHERE layer = 'memory' GROUP BY path "
        "ORDER BY 2 DESC, path ASC LIMIT ?",
        (limit,),
    ).fetchall()


def _stats_one_store(name, root, store_rel, top_access: int = 0) -> None:
    """Print one store's layer/classification breakdown. Existence is checked
    BEFORE opening (open_store would create an empty file otherwise); an existing
    store with zero rows is reported as unbuilt per the empty-matrix predicate."""
    store_path = root / store_rel
    label = f"{BOLD}Memory index{RESET} {GRAY}[{name}]{RESET}  {GRAY}{store_path}{RESET}"
    if not store_path.exists():
        print(label)
        print(f"  {YELLOW}not built yet{RESET} -- run: python3 scripts/memory-index.py build")
        return
    conn = open_store(root, store_rel)
    rows = conn.execute(
        "SELECT layer, COUNT(*), COUNT(DISTINCT path), MAX(dim) "
        "FROM notes GROUP BY layer ORDER BY layer"
    ).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    files = conn.execute("SELECT COUNT(DISTINCT path) FROM notes").fetchone()[0]
    fts_rows = conn.execute("SELECT COUNT(*) FROM notes_fts").fetchone()[0]
    last_mtime = conn.execute("SELECT MAX(mtime) FROM notes").fetchone()[0]
    cls_rows = conn.execute(
        "SELECT COALESCE(NULLIF(classification, ''), '(unset)'), COUNT(*) "
        "FROM notes GROUP BY 1 ORDER BY 1"
    ).fetchall()
    model = conn.execute("SELECT val FROM meta WHERE key='model'").fetchone()
    top_rows = top_access_rows(conn, top_access) if top_access > 0 else None
    conn.close()

    print(label)
    if total == 0:
        print(f"  {YELLOW}not built yet{RESET} -- run: python3 scripts/memory-index.py build")
        return
    print(f"  model:  {model[0] if model else '?'}")
    print(f"  notes:  {total} chunks across {files} files  {GRAY}(bm25 channel: {fts_rows}){RESET}")
    for layer, count, nfiles, dim in rows:
        chunked = f"  {GRAY}/ {nfiles} files{RESET}" if count != nfiles else ""
        print(f"    {CYAN}{layer:16}{RESET} {count:4}  dim={dim}{chunked}")
    print(f"  {BOLD}classification:{RESET}")
    for cls, count in cls_rows:
        print(f"    {CYAN}{cls:16}{RESET} {count:4}")
    if last_mtime:
        age_days = max(0.0, (time.time() - last_mtime) / 86400.0)
        print(f"  newest source: {last_mtime:.0f}  {GRAY}({age_days:.1f} days ago){RESET}")
    if top_rows is not None:
        print(f"  {BOLD}top {top_access} memories by access_count:{RESET}")
        if not top_rows:
            print(f"    {GRAY}no memory-layer entries in this store{RESET}")
        for path, count, last in top_rows:
            print(f"    {CYAN}{count:5}{RESET}  {path.rsplit('/', 1)[-1]:52}  "
                  f"{GRAY}last surfaced {last or 'never'}{RESET}")


# ============================================================
# CLI
# ============================================================

# ============================================================
# The store lock, and the glob helpers
# ============================================================
#
# The relocate and migration-window subcommands of the retired HEADING OS 2
# mover stood here until 2026-10-16. What remains is shared: the lock every
# build takes, `glob_to_regex` (also read by `scripts/hindsight-snapshot.py`
# for its self-check) and the layer matchers
# (`scripts/dev/derive-memory-index-globs.py`).

import fcntl
import fnmatch
from contextlib import contextmanager
from pathlib import PurePosixPath


def store_lock_path(root: Path, store_rel: str | Path) -> Path:
    """The lock a writer of `<root>/<store_rel>` holds: the db path plus `.lock`,
    beside the store and inside the same gitignored directory.

    `store_rel` arrives in TWO shapes and both are real, which is why the join
    is spelled through `str` rather than with `+` on the argument. The code
    target passes `CODE_STORE_REL`, a root-relative string. The content target
    passes `content_store_path(data_root, ...)`, an ABSOLUTE `Path`, since the
    store moved into the overlay's runtime-junk tree and that helper resolves
    it. `root / <absolute>` is that absolute path in `pathlib`, so one
    expression answers both; `<Path> + ".lock"` answers neither.

    MEASURED 2026-09-15 with the previous `root / (store_rel + ".lock")`:
    `cmd_build` raised `TypeError` before opening the store, so every content
    build died.
    """
    return Path(str(Path(root) / store_rel) + ".lock")


@contextmanager
def store_lock(root: Path, store_rel: str, *, what: str = "write"):
    """Hold an exclusive `flock` on the store for the duration of the block.

    Taken by every `build`: `memory-index-refresh.timer` runs one at :15 every
    hour, and two builds walking and pruning one store at once would delete
    each other's rows. SQLite's own `timeout` only serialises statements, not
    the walk-then-decide sequence around them. Blocking, with the wait said
    out loud.
    """
    path = store_lock_path(root, store_rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.stderr.write(
                f"{YELLOW}{what}: another writer holds {path}; "
                f"waiting for it to finish.{RESET}\n")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def glob_to_regex(pattern: str) -> "re.Pattern[str]":
    """The regex that accepts exactly the relative paths `Path.glob(pattern)`
    would list AS FILES, for one brace-free pattern.

    The wildcard and character-class syntax is NOT re-implemented here. Each
    path component is handed to `fnmatch.translate`, the same function pathlib
    itself translates a component with, and its output is then confined to one
    component: a bare `.` (what `?` and `*` become) is narrowed to `[^/]`, and
    a class is prefixed `(?!/)` so a negated one cannot claim a separator.
    pathlib matches one component against one name, so `contacts[!a]b.md`
    still cannot claim `contacts/b.md`.

    A hand-written translation stood here until 2026-09-14 and diverged from
    `Path.glob` in three ways, each MEASURED that day on Python 3.11.15:

    - `reference/[z-a].md`, an inverted range. `fnmatch` renders the class
      `(?!)` and `Path.glob` lists nothing; the hand-written branch emitted
      `[z-a]`, on which `re.compile` raises `bad character range`. A config
      carrying one crashed every build instead of matching
      nothing.
    - `reference/**`. A TERMINAL `**` selects directories, so `Path.glob`
      lists no file at all; the hand-written `.*` claimed every file beneath
      it.
    - `reference/**/*.md`. `Path.glob` lists `reference/dir\\n/file.md`, a
      directory component ending in a newline; `(?:.*/)?` cannot cross one,
      because `.` outside `DOTALL` does not match `\\n`.

    Kept from that version, because both are still true and both are easy to
    get wrong: a leading `^` in a class is LITERAL (fnmatch has no `^`
    negation; MEASURED 2026-09-14: `reference/[^a].md` lists `^.md` and
    `a.md`), and the match is the whole string with no trailing-newline
    allowance (`\\Z`, not `$`), since pathlib lists no file for `*.md` whose
    name ends in a newline.

    The three ways pathlib REFUSES a pattern are raised rather than answered,
    so a config that cannot be walked cannot be silently claimed either: an
    empty pattern, an absolute one, and `**` as part of a larger component.
    """
    if not pattern:
        raise ValueError("Unacceptable pattern: empty glob")
    parsed = PurePosixPath(pattern)
    if parsed.is_absolute():
        raise NotImplementedError("Non-relative patterns are unsupported")
    parts = parsed.parts
    if any("**" in part and part != "**" for part in parts):
        raise ValueError("Invalid pattern: '**' can only be an entire path component")
    # A trailing slash or a terminal `**` selects directories, never files.
    if pattern.endswith("/") or not parts or parts[-1] == "**":
        return re.compile(r"(?!)")

    out = []
    for index, part in enumerate(parts):
        # A `**` component carries its OWN trailing separator, because `**/`
        # may match nothing at all: `docs/**/*.md` lists `docs/x.md`.
        if part == "**":
            out.append("(?:[^/]+/)*")
            continue
        translated = fnmatch.translate(part)
        if not translated.startswith("(?s:") or not translated.endswith(r")\Z"):
            raise ValueError(f"unexpected fnmatch translation of {part!r}")
        body = translated[4:-3]
        piece = []
        i = 0
        while i < len(body):
            ch = body[i]
            if ch == "\\":                      # an escaped literal
                piece.append(body[i:i + 2])
                i += 2
            elif ch == "[":                     # a class: confine it, verbatim
                j = i + 1
                if j < len(body) and body[j] == "^":
                    j += 1
                if j < len(body) and body[j] == "]":
                    j += 1
                while j < len(body) and body[j] != "]":
                    j += 2 if body[j] == "\\" else 1
                if j >= len(body):
                    raise ValueError(f"unexpected fnmatch class in {part!r}")
                piece.append("(?!/)" + body[i:j + 1])
                i = j + 1
            elif ch == ".":                     # from `?` or `*`: one name only
                piece.append("[^/]")
                i += 1
            else:
                piece.append(ch)
                i += 1
        out.append("".join(piece))
        if index != len(parts) - 1:
            out.append("/")
    return re.compile(r"\A" + "".join(out) + r"\Z")


def layer_matchers(cfg: dict, layers) -> list:
    """[(layer, regex)] for the glob layers of one store, in config order, so a
    path's layer is the FIRST entry to match: the same first-claim rule the
    build applies while walking."""
    out = []
    for layer_cfg in cfg.get("layers", []):
        layer = layer_cfg.get("layer")
        if layer not in layers or layer_cfg.get("source") or not layer_cfg.get("glob"):
            continue
        for sub in expand_brace_glob(layer_cfg["glob"]):
            out.append((layer, glob_to_regex(sub)))
    return out


def layer_for_path(rel: str, matchers) -> str | None:
    for layer, rx in matchers:
        if rx.match(rel):
            return layer
    return None



def main() -> int:
    # Before anything reads the clock: `query --touch` resolves the debounce
    # date via get_default_tz(), which answers UTC unless HEADING_OS_TZ has
    # been loaded from .env first.
    load_env()
    parser = argparse.ArgumentParser(description="Local associative-memory index.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_build = sub.add_parser("build", help="(re)build the index from allowlisted layers")
    p_build.add_argument("--force", action="store_true", help="re-embed all, ignore mtime")
    p_build.add_argument("--allow-host-fallback", action="store_true",
                         help="build on the local daemon when no pinned embedder "
                              "answers; an incremental build there is refused like any "
                              "other embedder change, so pair it with --force")
    p_build.set_defaults(func=cmd_build)

    p_refresh = sub.add_parser(
        "refresh",
        help="the hourly job: write document companions, then an incremental build; "
             "non-zero when either half failed")
    p_refresh.set_defaults(func=cmd_refresh)

    p_query = sub.add_parser("query", help="semantic recall over the index")
    p_query.add_argument("text", help="query text (RU or EN)")
    p_query.add_argument("--layer", help="restrict to one layer (e.g. odin|thread|crm|outputs|skill); overrides --collection")
    p_query.add_argument("--collection", default="content",
                         help="layer group to search: content (default) | code | all")
    p_query.add_argument("--top-k", type=int, default=0, help="max hits (default from config)")
    p_query.add_argument("--threshold", type=float, default=None, help="min score (default from config)")
    p_query.add_argument("--json", action="store_true", help="emit machine-readable JSON (hits + gap object)")
    p_query.add_argument("--touch", action="store_true",
                         help="reinforce confident auto-memory hits (bumps access_count; "
                              "this flag writes at most once per file per day, but other "
                              "callers of scripts/memory-touch.py are not debounced)")
    p_query.set_defaults(func=cmd_query)

    p_stats = sub.add_parser("stats", help="index summary by layer")
    p_stats.add_argument("--top-access", type=int, nargs="?", const=20, default=0,
                         metavar="N",
                         help="also list the N most-accessed memories (default 20) with "
                              "access_count and last_accessed; a diagnostic on the "
                              "reinforcement loop, never a removal worklist")
    p_stats.set_defaults(func=cmd_stats)

    args = parser.parse_args()
    # Only build and refresh write under the data root; the read paths stay open
    # from a YARD. Asked of the resolved handler rather than of `args.cmd`, so a
    # renamed subcommand cannot silently unguard the write path.
    if args.func in (cmd_build, cmd_refresh):
        require_main_clone(__file__)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
