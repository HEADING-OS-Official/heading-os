"""Where a runtime store is, when it may still sit at a pre-move address.

What is left of the one-time runtime move after the mover was retired on
2026-09-28 (HOS-166; the mover, its CLI and its 64-row table are archived in
the data repository under registry name `system-code`, in `archive/`, zip
`2026-09-28-decision-map-removals.zip`). One question is still asked live:
`store_address`, for the Action Queue (`scripts/action-queue.py`,
`scripts/record_readers/sources/action_queue.py`), answers where a store's
marker file actually is, the current address or an address the move and the
HEADING OS 2 family migration left it at, and refuses to choose when both hold
one (`AmbiguousStore`).

Tests: none live.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from scripts.utils.runtime_dir import store_dirname
from scripts.heading_os_2.kinds import homes_under


#: The one pre-move address a live reader still asks about, keyed by the
#: store's place under the runtime store root (`<service>/<kind>/<subpath>`).
#: It is the only row of the retired mover's table that a live call reaches
#: (2026-10-16): the Action Queue store sat here before the move, and the
#: HEADING OS 2 family move may have carried it under a family root since.
#: A registry name cannot replace it: the registry answers where a record IS,
#: and this is where it WAS. Why the reader asks at all: on 2026-09-20 the
#: queue's cards sat at this address while the reader composed the post-move
#: one and showed an empty queue.
LEGACY_STORE_PREFIXES: dict[str, str] = {
    "action-queue/state/action-queue": "outputs/operations/action-queue",  # leak-guard: ok (the pre-move address a live reader still checks)
}

#: A former place of a store INSIDE the runtime store root, keyed the same way.
#: The `bridge` service, named for the daemon removed 2026-09-21 (HOS-7), was
#: folded into `action-queue` on 2026-10-16 (F-2.C18). HELM moves the directory
#: by hand; until then the reader finds the store here, and once moved this row
#: answers nothing and can go.
FORMER_RUNTIME_PLACES: dict[str, str] = {
    "action-queue/state/action-queue": "bridge/state/action-queue",
}


class AmbiguousStore(Exception):
    """Two addresses hold the store file, and nothing here may pick one.

    A silent pick is the failure this whole module exists around: for the Action
    Queue it decides which drafted sends the operator is shown, and approving a
    card is irreversible. The caller reports both paths and stops.
    """


@dataclass(frozen=True)
class StoreAddress:
    """Where a runtime store is, versus where the mover would put it."""

    path: Path          # the directory to read and write, right now
    current: Path       # the runtime store's `<service>/<kind>[/<subpath>]`
    found: bool         # the marker file was found at `path`

    @property
    def legacy(self) -> bool:
        """True when the store is still at a pre-move address."""
        return self.found and self.path != self.current


def legacy_prefixes(store_rel: str, store: str) -> tuple[str, ...]:
    """The data-root-relative directories whose contents belong at `store_rel`.

    `store_rel` is a store directory spelled from the data root, e.g.
    `<store>/<service>/<kind>/<subpath>`. Empty for any store directory
    neither `LEGACY_STORE_PREFIXES` nor `FORMER_RUNTIME_PLACES` names, so such
    a reader keeps today's behaviour.
    """
    head = f"{store}/"
    if not store_rel.startswith(head):
        return ()
    key = store_rel[len(head):]
    found = []
    if key in LEGACY_STORE_PREFIXES:
        found.append(LEGACY_STORE_PREFIXES[key])
    if key in FORMER_RUNTIME_PLACES:
        found.append(head + FORMER_RUNTIME_PLACES[key])
    return tuple(found)


def store_address(current: Path, marker: str, *, data_root: Path) -> StoreAddress:
    """Where the store holding `marker` is: the current address, or a pre-move one.

    `current` is where `data_write_dir` puts it. The answer is decided by which
    candidate directory actually holds `marker`, never by which directory
    exists: a legacy directory can survive the move holding records that are not
    this store at all.

    A write lands at `.path`, beside the file the read came from, and this
    function never relocates anything. The store has ONE address per run, so the
    reader, every writer and the `queue.json.lock` sidecar agree; a write to the
    new address while the old one is live would split the store into two halves
    under two locks, and relocating the operator's records is HELM's job, by
    hand, with the daemons stopped (the mover that did it was retired
    2026-09-28).

    Raises `AmbiguousStore` when more than one candidate holds `marker`.

    Costs one `is_file` per candidate plus, when `current` is bare, one
    `moved_homes` scan: MEASURED 2026-09-20 on the operator's overlay at
    0.0009 s, against a 176 KB JSON read on the other side of it.

    HOS-5 phase 6 kept the `moved_homes` call below, and the question it asks is
    the reason. The front door answers where a store IS, which is `current` and
    already the first candidate, and by design it carries no fallback to a
    pre-move address; this loop wants exactly the pre-move addresses, so there is
    nothing to convert it to. The fan-out is real as well: MEASURED 2026-09-22
    over the prefix rules, `outputs/operations/action-queue` reaches TWO homes  # leak-guard: ok (measurement: the addresses as they were on the day measured)
    (`trace/report/...` and `trace/scratch/...`), and a candidate list that named  # leak-guard: ok (measurement: the addresses as they were on the day measured)
    one of them would report a store sitting at the other as absent, which for
    this function means silently starting a second copy of it.
    """
    data_root = Path(data_root)
    current = Path(current)
    candidates: list[Path] = [current]
    try:
        rel = current.relative_to(data_root).as_posix()
    except ValueError:
        rel = ""                      # not under this data root; it cannot have moved
    # Not `data_path`: the front door answers where the store IS (`current`),
    # and this loop wants where it WAS, which can fan out to two homes (2026-09-22).
    for prefix in legacy_prefixes(rel, store_dirname(data_root)):
        for home in homes_under(data_root / prefix, data_root=data_root):
            if home not in candidates:
                candidates.append(home)
    holding = [c for c in candidates if (c / marker).is_file()]
    if len(holding) > 1:
        raise AmbiguousStore(
            f"{marker} exists at {len(holding)} addresses and nothing may choose "
            f"between them: " + ", ".join(str(p) for p in holding) +
            ". HELM decides which is the store and moves the other aside by "
            "hand, with the daemons stopped.")
    return StoreAddress(holding[0] if holding else current, current, bool(holding))
