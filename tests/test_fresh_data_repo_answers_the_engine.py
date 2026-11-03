#!/usr/bin/env python3
"""GUARANTEE: every data-address name the engine asks for is answered on a freshly created data repository, or is named as exempt.

FAILURE B, the engine acting on a data address that does not exist in this
repository. A data repository created by `scripts/create-data-repo.py` carries
no `names:` block, so every name is answered from the engine's shipped
templates. HOS-298, MEASURED 2026-11-01 on a tree scaffolded by
`scripts/init-data.py` at commit 5656d723: `runtime:settings-backup/state`,
`threads-personal`, `odin-positions`, `odin-sources` and `odin-episodes` all
refused, so a new executive's install ran without a settings backup, without
`thread.py list` and without the Odin memory layer, and said so to nobody.

The guard is `scripts/check-engine-data-names.py`; both halves drive its real
entry point and assert its exit code. The refusing half runs it over the engine
tree as it stood at 5656d723, extracted with `git archive` under `tmp_path`,
outside the checkout (K011), so the old templates are held to today's question.
"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.workspace import get_workspace_root  # noqa: E402

GUARD = Path("scripts") / "check-engine-data-names.py"
#: The last engine commit before HOS-298 added the missing rows.
BEFORE_FIX = "5656d723"


def _guard(root: Path, tree: Path, data: Path) -> subprocess.CompletedProcess:
    # A throwaway data root, so the child never has the operator's live one.
    data.mkdir(exist_ok=True)
    return subprocess.run(
        [sys.executable, str(root / GUARD), "--check", "--root", str(tree)],
        cwd=root, capture_output=True, text=True, timeout=300,
        env=dict(os.environ, HEADING_OS_DATA=str(data)))


def test_the_shipped_templates_answer_every_name_the_engine_asks_for(tmp_path):
    root = get_workspace_root()
    proc = _guard(root, root, tmp_path / "data")
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_the_guard_and_the_installer_refuse_the_same_installer_address(tmp_path):
    """HOS-298 review round 2, MEASURED 2026-11-01 at f586cb99: with the
    `threads-personal` address moved outside the subject roots, the guard exited
    0 while `scripts/init-data.py` refused to scaffold. Both now ask
    `data_path.installer_address_problems`. Driven on a copy of this checkout's
    tracked files under `tmp_path`: as shipped both say yes; with the address
    moved both say no."""
    root = get_workspace_root()
    listing = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True,
                             check=True).stdout.decode().split("\0")
    copy = tmp_path / "engine"
    for rel in filter(None, listing):
        if (root / rel).is_file():
            (copy / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / rel, copy / rel)
    subprocess.run(["git", "init", "-q"], cwd=copy, check=True)
    subprocess.run(["git", "add", "-A"], cwd=copy, check=True, timeout=120)
    env = dict(os.environ, HEADING_OS_DATA=str(tmp_path / "data"))

    def scaffold(target: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(copy / "scripts" / "init-data.py"), "--path",
             str(tmp_path / target)], cwd=copy, capture_output=True, text=True,
            timeout=120, env=env)

    assert _guard(root, copy, tmp_path / "data").returncode == 0
    assert scaffold("fresh-honest").returncode == 0

    installer = copy / "scripts" / "init-data.py"
    text = installer.read_text(encoding="utf-8")
    assert text.count('(("personal", "threads"),') == 1
    installer.write_text(text.replace('(("personal", "threads"),',
                                      '(("elsewhere", "threads"),'), encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=copy, check=True)

    proc = _guard(root, copy, tmp_path / "data")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "would refuse its address" in re.sub(r"\x1b\[[0-9;]*m", "", proc.stdout)
    refused = scaffold("fresh-moved")
    assert refused.returncode != 0 and "Refusing to scaffold" in refused.stdout + refused.stderr
