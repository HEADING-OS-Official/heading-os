"""The data repository beside an engine clone is found by ONE rule, and every guard watches what it finds.

HOS-313, operator decision 2026-11-01 (option a). The executive layout puts the
data repository at `<root>/data` beside the engine at `<root>/engine`. Before
this change `get_data_root()` and the three structural guards knew one sibling
name only, `.heading-os-data`. MEASURED 2026-11-01 on the code of `a51ba507`,
in the layout built below with `data/.schema-version` present and no
`HEADING_OS_DATA`: `get_data_root()` answered `<root>/engine/examples` (demo),
an entry-point-less write into `<root>/data/records/` LANDED through the armed
overlay write guard, `_yard_data_roots()` did not name `<root>/data`, and the
runtime write guard watched no store under it.

Failure B: the data repository is the live thing on an executive machine, and a
guard that watches nothing there is the merge of the two trees by omission.

Three layouts and both directions, per M031.1:
  * `engine` + stamped `data`: resolves and is guarded (red before HOS-313);
  * `engine` + `data` with no stamp, a stamp that is not a version, or a
    checkout folder not named `engine`: does NOT resolve, and a write there is
    an ordinary write (green before and after);
  * `.heading-os-data`: unchanged, and it wins over a stamped `data`;
  * a stamp that EXISTS but cannot be read (mode 000) or decoded (UTF-16, a
    UTF-8 BOM) resolves to `data` and is guarded. MEASURED 2026-11-01 on
    `09658a62` (HELM review round 1): mode 000 raised PermissionError, the
    guard's handler answered "no overlay", and the write into `data/records/`
    landed; the UTF-16 and BOM stamps landed the write the same way.

Every tree is under `tmp_path`; the guard copies under test believe the
throwaway tree is the operator's data, as in `_pretend_world` of
`tests/test_yard_containment.py`.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REFUSED = 3

# Loads the copied `paths.py` and `overlay_write_guard.py` by path, prints the
# data root, then writes from `-c`, an entry point with no file behind it, which
# GUARD mode refuses inside a watched root and allows everywhere else.
CHILD = """
import importlib.util, sys
from pathlib import Path
utils = Path(sys.argv[1]) / "scripts" / "utils"
def load(name):
    spec = importlib.util.spec_from_file_location("_hos313_" + name, utils / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
try:
    print(load("paths").get_data_root())
except OSError as exc:
    print("get_data_root raised", type(exc).__name__)
guard = load("overlay_write_guard")
guard.arm(guard.MODE_GUARD, structural_only=True)
try:
    with open(sys.argv[2], "w") as fh:
        fh.write("ZZQX\\n")
except guard.OverlayWriteRefused:
    sys.exit(3)
"""

# (layout, checkout folder name, legacy sibling?, stamp text or None, the data
# root expected: "data", "legacy" or None for demo). A stamp of UNREADABLE is a
# valid stamp written and then made mode 000.
UNREADABLE = "1\n\0unreadable"
LAYOUTS = [
    ("engine-and-stamped-data", "engine", False, "1\n", "data"),
    ("engine-and-an-unreadable-stamp", "engine", False, UNREADABLE, "data"),
    ("engine-and-a-utf16-stamp", "engine", False, b"\xff\xfe1\x00", "data"),
    ("engine-and-a-bom-stamp", "engine", False, b"\xef\xbb\xbf1\n", "data"),
    ("engine-and-unstamped-data", "engine", False, None, None),
    ("engine-and-a-stamp-that-is-not-a-version", "engine", False, "zzqx\n", None),
    ("stamped-data-beside-a-folder-not-named-engine", "heading", False, "1\n", None),
    ("heading-os-data-sibling", "engine", True, None, "legacy"),
    ("heading-os-data-wins-over-stamped-data", "engine", True, "1\n", "legacy"),
]


def _world(tmp_path: Path, name: str, legacy: bool, stamp: str | None) -> dict:
    world = tmp_path / "world"
    engine = world / name
    (engine / "scripts" / "utils").mkdir(parents=True)
    (engine / ".claude").mkdir()
    (engine / "CLAUDE.md").write_text("", encoding="utf-8")
    for module in ("paths.py", "overlay_write_guard.py", "excluded_dirs.py"):
        source = ROOT / "scripts" / "utils" / module
        (engine / "scripts" / "utils" / module).write_bytes(source.read_bytes())
    data = world / "data"
    (data / "records").mkdir(parents=True)
    if isinstance(stamp, bytes):
        (data / ".schema-version").write_bytes(stamp)
    elif stamp is not None:
        (data / ".schema-version").write_text(stamp.split("\0")[0], encoding="utf-8")
    if stamp == UNREADABLE:
        (data / ".schema-version").chmod(0)
    roots = {"data": data.resolve(), "legacy": None}
    if legacy:
        (world / ".heading-os-data" / "records").mkdir(parents=True)
        roots["legacy"] = (world / ".heading-os-data").resolve()
    return {"engine": engine, "roots": roots}


def _child(engine: Path, target: Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items()
           if k not in ("HEADING_OS_DATA", "WORKSPACE_ROOT", "HEADING_OS_OVERLAY_GUARD")}
    return subprocess.run([sys.executable, "-c", CHILD, str(engine), str(target)],
                          capture_output=True, text=True, env=env, cwd=str(engine),
                          timeout=120)


@pytest.mark.parametrize("layout,name,legacy,stamp,expected", LAYOUTS,
                         ids=[row[0] for row in LAYOUTS])
def test_the_data_repository_beside_an_engine_is_found_by_one_rule_and_guarded(
        tmp_path, monkeypatch, layout, name, legacy, stamp, expected):
    if stamp == UNREADABLE and os.geteuid() == 0:
        pytest.skip("root reads a mode-000 file, so this layout cannot be built")
    world = _world(tmp_path, name, legacy, stamp)
    engine, roots = world["engine"], world["roots"]
    want = roots[expected] if expected else None
    data = roots["data"]

    # 1. get_data_root() and the overlay write guard, in a process of their own.
    target = (want or data) / "records" / "zzqx.md"
    run = _child(engine, target)
    if want is not None:
        assert run.returncode == REFUSED, f"{layout}: the guard let the write land"
        assert not target.exists(), f"{layout}: bytes landed in the data repository"
    else:
        assert run.returncode == 0, f"{layout}: an ordinary write was refused: {run.stderr}"
        assert target.exists(), f"{layout}: allowed, but nothing was written"
    resolved = run.stdout.splitlines()[0] if run.stdout else None
    assert resolved == str(want or (engine / "examples").resolve()), (layout, run.stderr)

    # 2. The yard wall names the sibling it finds, and only that one.
    spec = importlib.util.spec_from_file_location(
        "hos313_dispatch", ROOT / ".claude" / "hooks" / "_dispatch.py")
    dispatch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dispatch)
    yard_roots = dispatch._yard_data_roots(engine)
    assert (want in yard_roots) if want else (data not in yard_roots), (layout, yard_roots)
    if expected == "legacy":
        assert data not in yard_roots, (layout, yard_roots)

    # 3. The runtime write guard watches a store under what the rule found.
    from scripts.utils import runtime_write_guard
    monkeypatch.setattr(runtime_write_guard, "ENGINE_ROOT", engine)
    watched_parents = {root.parent for root in runtime_write_guard.watched_store_roots()}
    if want is not None:
        assert want in watched_parents, (layout, watched_parents)
    if expected != "data":
        assert data not in watched_parents, (layout, watched_parents)
