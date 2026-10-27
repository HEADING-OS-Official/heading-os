"""A throwaway DATA repository, complete enough that every resolver answers over it.

    from scripts.utils.synthetic_data_root import build_synthetic_data_root
    root = build_synthetic_data_root(tmp_path / "synthetic")

    SCRATCH="$(python scripts/data-path.py path --create runtime:scratch/tmp)"
    python scripts/data-path.py synthetic "$SCRATCH/synthetic-overlay"   # the same, from a shell

## Why this exists

HOS-5 requires that the public engine's own checks run against a synthetic
temporary DATA repository, without reaching the operator's private overlay. A
public clone, CI and a service host's pull-only copy have no overlay of their
own, so without a synthetic repository the agreement between `data_path()` and
the registry could only ever be checked on one machine, by one person.

## The layout, since HOS-240 (2026-10-13)

The C3 subject tree, the layout `scripts/init-data.py` scaffolds a fresh data
repository with, and every name a `path:` (or `runtime:`) entry. Until that day
this module built the pre-C3 HEADING OS 2 layout, wrote the family manifest
through `scripts/heading_os_2/manifest.py` and shipped `record:` entries, so the
record resolver had something to answer over. No engine site asks a record kind
any more, so the tree no longer carries a manifest, and the history of that
version is in git.

## The tree it builds

Small on purpose, and each file is there to make a different branch answerable
rather than to look like a real workspace:

    system/reports/...        a report directory, the shape most writers use  # leak-guard: ok (synthetic registry: the address book of a throwaway tree, never the real one)
    company/reference/...     a reference page  # leak-guard: ok (synthetic registry: the address book of a throwaway tree, never the real one)
    people/_index/...         a single named file a `path:` entry addresses  # leak-guard: ok (synthetic registry: the address book of a throwaway tree, never the real one)
    people/<slug>.md          the parameterised address, one file per person  # leak-guard: ok (synthetic registry: the address book of a throwaway tree, never the real one)
    knowledge/odin-brain/...  a knowledge note  # leak-guard: ok (synthetic registry: the address book of a throwaway tree, never the real one)
    auto-memory/MEMORY.md     an address outside the subject roots  # leak-guard: ok (synthetic registry: the address book of a throwaway tree, never the real one)
    runtime-junk/browser/...  runtime state, so `runtime:` answers here too
    config/data-paths.yaml    the registry itself, at the one constant address,
                              carrying BOTH blocks: the invented `names:` below
                              and the real `services:` block copied from
                              `scripts/runtime-services.example.yaml`

Nothing in it is real. The names are invented and the contents are one line
each; this repository is public and a fixture that looked like the operator's
tree would be a leak with a test's name on it.
"""
from __future__ import annotations

from pathlib import Path

from scripts.utils.data_path import REGISTRY_REL
from scripts.utils.runtime_dir import example_services_block

#: The files the tree is built from: relative path -> the one line it holds.
#: Spelled as data-relative addresses and never joined to an engine root; each
#: is created under the caller's `dest`.
SEED_FILES: dict[str, str] = {  # leak-guard: ok (fixture addresses under a caller's dest)
    "system/reports/synthetic/2026-09-19-a-synthetic-report.md": "# A report\n\nInvented.\n",  # leak-guard: ok (fixture address, joined to the caller's dest)
    "company/reference/a-synthetic-note.md": "# A note\n\nInvented.\n",  # leak-guard: ok (fixture address, joined to the caller's dest)
    "people/_index/crm-config.md": "# CRM configuration\n\nInvented.\n",  # leak-guard: ok (fixture address, joined to the caller's dest)
    "people/example-person.md": "# Example Person\n\nInvented.\n",  # leak-guard: ok (fixture address, joined to the caller's dest)
    "knowledge/odin-brain/principles/a-synthetic-principle.md": "# A principle\n\nInvented.\n",  # leak-guard: ok (fixture address, joined to the caller's dest)
    "auto-memory/MEMORY.md": "# Memory index\n\nInvented.\n",  # leak-guard: ok (fixture address, joined to the caller's dest)
    "runtime-junk/browser/cache/cookies.json": "[]\n",  # leak-guard: ok (fixture address, joined to the caller's dest)
}

#: The registry this tree ships. A SUBSET of the shapes the real one carries,
#: one per base, so a check that runs against both trees exercises the same
#: code paths. Written as text rather than dumped from a dict, because
#: `scripts/utils/yamlio.py` reads YAML and does not write it, and a second YAML
#: writer is not worth owning for these lines.
REGISTRY_TEXT = """\
# A synthetic data-address registry. Nothing here is real.
#
# Built by scripts/utils/synthetic_data_root.py so that the engine's own checks
# can run without the operator's private overlay.

version: 1

names:

  crm-config:
    path: people/_index/crm-config.md  # leak-guard: ok (synthetic registry: the address book of a throwaway tree, never the real one)
    what: the CRM's own configuration file

  crm-contact:
    path: people/{person}.md  # leak-guard: ok (synthetic registry: the address book of a throwaway tree, never the real one)
    what: one contact card, addressed by the contact's slug

  synthetic-reports:
    path: system/reports/synthetic  # leak-guard: ok (synthetic registry: the address book of a throwaway tree, never the real one)
    what: a report directory

  memory-index:
    path: auto-memory/MEMORY.md  # leak-guard: ok (synthetic registry: the address book of a throwaway tree, never the real one)
    what: the resident memory index, an address outside the subject roots

  browser-cookies:
    runtime: browser/cache
    under: cookies.json
    what: the cookie jar the headless browser reuses
"""

#: Written so `require_writable_data_root()` is content on this tree. A missing
#: marker already reads as current, so this is a statement rather than a repair.
SCHEMA_VERSION_FILE = ".schema-version"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def build_synthetic_data_root(dest: Path, *, registry: bool = True) -> Path:
    """Create the tree at `dest` and return it. Existing files are overwritten.

    `registry=False` leaves `config/data-paths.yaml` out, which is the state of a
    tree with no registry of its own, answered from the engine's default names.
    A caller testing that fallback wants that shape.
    """
    dest = Path(dest)
    for rel, text in SEED_FILES.items():
        _write(dest / rel, text)
    _write(dest / SCHEMA_VERSION_FILE, "1\n")
    if registry:
        # The `services:` block is APPENDED from the engine's own template
        # rather than restated here. The runtime vocabulary moved under the data
        # root on 2026-09-19 (HOS-5 § 7.2), so a synthetic tree without it
        # answers no runtime name at all -- and a hand-written copy of the 25
        # rows in a fixture would be exactly the second registry the fold
        # removed, drifting the first time somebody registers a service.
        _write(dest / REGISTRY_REL, REGISTRY_TEXT + "\n" + example_services_block())

    return dest


__all__ = ["REGISTRY_TEXT", "SEED_FILES", "build_synthetic_data_root"]
