"""GUARANTEE: a data-repository path is private, an engine path is engine only by the allowlist, and everything else is REFUSED.

FAILURE A, and it is the widest one in this suite. Every other check names a
thing that already exists. This one answers for the path nobody has created yet:
the classification resolver is what every publish, push and build path asks
before a byte leaves the overlay, and the only answers that are safe for a path
no list covers are the closed ones.

THE CONTRACT SINCE HOS-232 (2026-10-12), privacy by repository:

    in the data repository                     -> private
    in the engine AND in the engine allowlist  -> engine
    in the engine and NOT in the allowlist     -> RoutingRefused('boundary')
    outside both, unresolvable or ambiguous    -> RoutingRefused(<why>)

The data repository's own rule file is no longer read. The engine answer needs
BOTH `config/engine-allowlist.yaml` (the operator's definition, HOS-131) and
`config/routing-map.yaml` to say engine.

HOW IT IS DRIVEN. `get_routing_destination()` in `scripts/utils/workspace.py`,
over the real files for the live cases, and in a SUBPROCESS under
`WORKSPACE_ROOT` and `HEADING_OS_DATA` pointed at scratch trees for the
controlled ones, because both lists are cached on file identity and a test must
never edit a live list to learn what a broken one does.

THE REPORTING SURFACE IS PART OF THIS GUARANTEE, since 2026-09-23.
`scripts/classification-health.py` is the place a human looks; the last two
functions drive it, and nothing else in this repository does.

NOT CLAIMED. Nothing about whether any particular real file is in the right
tree, and nothing about what the publish and push paths DO with the answer.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.utils.workspace import (  # noqa: E402
    get_routing_destination, load_routing_map,
)

# Invented lists, so nothing in this public repository restates what the live
# ones route. ZZQX marks every invented value in this file.
INVENTED_ENGINE_MAP = (
    "version: 1\n"
    "default: private\n"
    "data_default: private\n"
    "rules:\n"
    '  "zzqx-code/": engine\n'
    '  "zzqx-code/zzqx-runtime.py": private\n'
)
INVENTED_ALLOWLIST = (
    "classes:\n"
    "  - class: zzqx-code\n"
    "    reason: invented\n"
    "    patterns:\n"
    '      - "zzqx-code/**/*.py"\n'
)

# Not YAML. The loaders have to fail on it, which is the input this exists for.
UNPARSEABLE_MAP = 'version: 1\nrules:\n  "zzqx/": [\n   unclosed: {\n'


def _trees(tmp_path: Path, engine_map: str = INVENTED_ENGINE_MAP,
           allowlist: str | None = INVENTED_ALLOWLIST) -> tuple[Path, Path]:
    """A scratch engine root and a scratch data root the resolver accepts.

    The data root must be neither the engine root nor `<engine>/examples`, or
    `data_overlay_present()` answers False and there is no second tree.
    """
    eng, dat = tmp_path / "zzqx-engine", tmp_path / "zzqx-data"
    (eng / ".claude").mkdir(parents=True)
    (eng / "config").mkdir()
    (eng / "zzqx-code").mkdir()
    (eng / "CLAUDE.md").write_text("ZZQX scratch root\n", encoding="utf-8")
    (eng / "config" / "routing-map.yaml").write_text(engine_map, encoding="utf-8")
    if allowlist is not None:
        (eng / "config" / "engine-allowlist.yaml").write_text(allowlist, encoding="utf-8")
    (dat / "config").mkdir(parents=True)
    (dat / "zzqx-family").mkdir()
    return eng, dat


def _resolve_under(eng: Path, dat: Path, cases: list) -> dict[str, str]:
    """Run the REAL resolver in a subprocess whose roots are `eng` and `dat`.

    Each case is `(path, tree)`; the answer is the destination, or
    `REFUSED:<reason>` for the contract's third answer. The refusal class is
    looked up rather than imported, so the same cases can be put to a version
    that predates it and answer there instead of failing to start.
    """
    program = (
        "import json,sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from scripts.utils import workspace as w\n"
        "refused = getattr(w, 'RoutingRefused', ())\n"
        "def ask(p, t):\n"
        "    try:\n"
        "        return w.get_routing_destination(p, tree=t)\n"
        "    except refused as exc:\n"
        "        return 'REFUSED:' + exc.reason\n"
        "print(json.dumps({p + '|' + str(t): ask(p, t) for p, t in json.loads(sys.argv[1])}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", program, json.dumps(cases)],
        cwd=str(ROOT), capture_output=True, text=True, timeout=120,
        env={"WORKSPACE_ROOT": str(eng), "HEADING_OS_DATA": str(dat),
             "PATH": "/usr/bin:/bin"},
    )
    if result.returncode != 0:
        pytest.fail("the resolver could not be driven against the scratch trees:\n"
                    f"{result.stdout}\n{result.stderr}")
    return json.loads(result.stdout)


def _tracked() -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=str(ROOT),
                         capture_output=True, timeout=120)
    return out.stdout.decode("utf-8", "replace").splitlines()


@pytest.fixture(scope="module")
def live_rules() -> dict[str, str]:
    """The live rule set, read at run time rather than copied. FAILS when empty:
    a test that cannot find the thing it tests has learned nothing."""
    rules = load_routing_map().get("rules") or {}
    if not rules:
        pytest.fail(f"the routing map yielded no rules at "
                    f"{ROOT / 'config' / 'routing-map.yaml'}, so every case "
                    f"below would pass over an empty corpus")
    return rules


def test_the_public_map_names_no_address_outside_this_repository(live_rules):
    """Every key in the PUBLIC map starts with a top-level name this repository
    tracks. A key that does not is an address in the private overlay, and
    writing one here discloses it whether or not any gate refuses the file.
    The 2026-09-21 split exists for this; HOS-232 left the map engine-only."""
    tracked = {line.partition("/")[0] for line in _tracked()}
    assert len(tracked) > 5, (
        f"git reported only {len(tracked)} top-level names, so the comparison "
        f"below would convict everything; the corpus is wrong, not the map")
    foreign = sorted(k for k in live_rules
                     if k.lstrip("/").partition("/")[0] not in tracked)
    assert not foreign, (
        f"{len(foreign)} key(s) in the PUBLIC routing map name a path this "
        f"repository does not track, which discloses a private address: "
        f"{foreign[:5]}")


def test_the_data_repository_rule_file_is_not_read_and_cannot_open_anything(tmp_path):
    """A rule file in the data repository changes no answer, in either direction.

    BIDIRECTIONAL: against the version before HOS-232 the overlay's
    `config/routing-map-private.yaml` was READ, so its `zzqx-code/zzqx-closed.py`
    rule turned an allowlisted engine file `private`, and this goes red on the
    second assertion. The first is the property the file existed to keep: an
    `engine` value there never opened a data path.
    """
    eng, dat = _trees(tmp_path)
    (dat / "config" / "routing-map-private.yaml").write_text(
        'rules:\n  "zzqx-family/": engine\n  "zzqx-code/zzqx-closed.py": private\n',
        encoding="utf-8")
    got = _resolve_under(eng, dat, [("zzqx-family/f.md", "data"),
                                    ("zzqx-code/zzqx-closed.py", "engine")])
    assert got["zzqx-family/f.md|data"] == "private"
    assert got["zzqx-code/zzqx-closed.py|engine"] == "engine", (
        "the data repository's rule file is still being read")


def test_an_unlisted_path_is_private_in_data_refused_in_engine_and_engine_code_still_publishes(live_rules):
    """The contract, and the honest case that must still pass beside it.

    An engine path no list covers is REFUSED, never quietly `private`: until
    HOS-232 it answered `private`, which every gate read exactly like data and
    reported as "belongs in the data repo". A data path is `private` by
    repository. The second half is what stops a resolver hard-wired to refuse:
    every tracked engine file must still resolve `engine`, over the real corpus.
    """
    from scripts.utils.workspace import RoutingRefused

    unlisted = "zzqx-nobody-listed-this/zzqx-note.md"
    assert get_routing_destination(unlisted, tree="data") == "private"
    with pytest.raises(RoutingRefused) as refused:
        get_routing_destination(unlisted, tree="engine")
    assert refused.value.reason == "unresolvable"

    tracked = _tracked()
    assert len(tracked) > 500, (
        f"git reported {len(tracked)} tracked files, so the loop below would "
        f"pass over an almost empty corpus")
    not_engine = [p for p in tracked
                  if get_routing_destination(p, tree="engine") != "engine"]
    assert not not_engine, (
        f"{len(not_engine)} tracked engine file(s) no longer resolve 'engine', so "
        f"the commit gate would refuse this repository's own code: {not_engine[:5]}")


def test_no_rule_can_make_a_data_path_engine(live_rules):
    """Asked of every rule the live map carries, so a rule added tomorrow is
    covered tomorrow: under the data tree its key answers `private`."""
    leaked = [key for key in live_rules
              if get_routing_destination(key.rstrip("/") + "/zzqx.md",
                                         tree="data") != "private"]
    assert not leaked, (
        f"{len(leaked)} routing rule(s) resolve a data path to something other "
        f"than 'private': {leaked[:5]}")


def test_engine_needs_both_lists_and_a_public_clone_loses_nothing(tmp_path):
    """`engine` only where the allowlist AND the routing map both say so.

    `zzqx-code/zzqx-runtime.py` is covered by the allowlist's pattern and
    closed by the map's private exception; `zzqx-code/notes.md` is opened by
    the map's directory rule and not covered by the allowlist. Both refuse.
    Before HOS-232 the second answered `engine`. The public-clone half: with
    no data root at all the engine answers are unchanged.
    """
    eng, dat = _trees(tmp_path)
    (eng / "zzqx-code" / "notes.md").write_text("zzqx\n", encoding="utf-8")
    cases = [("zzqx-code/main.py", "engine"), ("zzqx-code/zzqx-runtime.py", "engine"),
             ("zzqx-code/notes.md", "engine"), ("zzqx-family/f.md", "data")]
    got = _resolve_under(eng, dat, cases)
    assert got["zzqx-code/main.py|engine"] == "engine"
    assert got["zzqx-code/zzqx-runtime.py|engine"] == "REFUSED:unresolvable"
    assert got["zzqx-code/notes.md|engine"] == "REFUSED:boundary", (
        "a directory rule in the map opened a file the allowlist does not cover")
    assert got["zzqx-family/f.md|data"] == "private"
    (eng / "examples").mkdir()
    public = _resolve_under(eng, eng / "examples", cases)
    assert public == got


def test_a_broken_map_or_allowlist_answers_engine_for_nothing(tmp_path):
    """The hard fail-closed cases: the state in which the resolver knows least
    is the one in which answering `engine` would be worst. Either list broken
    or absent, and no engine path answers `engine`; the data path stays
    `private`."""
    for name, engine_map, allowlist in (
            ("map", UNPARSEABLE_MAP, INVENTED_ALLOWLIST),
            ("allowlist", INVENTED_ENGINE_MAP, UNPARSEABLE_MAP),
            ("absent", INVENTED_ENGINE_MAP, None)):
        eng, dat = _trees(tmp_path / name, engine_map, allowlist)
        got = _resolve_under(eng, dat, [("zzqx-code/main.py", "engine"),
                                        ("zzqx-family/f.md", "data")])
        assert got["zzqx-code/main.py|engine"].startswith("REFUSED:"), (name, got)
        assert got["zzqx-family/f.md|data"] == "private", (name, got)


def test_adversarial_paths_are_refused_and_honest_ones_still_answer(tmp_path):
    """Relative paths, `..`, a link into the data root, deleted paths, a nested
    repository, an absolute path elsewhere and a not-yet-written target.

    MEASURED 2026-10-12 against the version before HOS-232: the `..` path that
    leaves for the data tree answered `engine` (a prefix match on the string),
    as did the link into the data root, the nested repository's file and the
    file that exists in both trees, asked bare and as engine; the deleted
    unlisted file and the absolute path elsewhere answered `private`, which no
    gate tells apart from data. The honest half must keep answering:
    a deleted allowlisted file and a new write target are engine, a record about
    to be written under a data-only family is private.
    """
    eng, dat = _trees(tmp_path)
    (dat / "zzqx-family" / "f.md").write_text("zzqx private\n", encoding="utf-8")
    os.symlink(dat / "zzqx-family" / "f.md", eng / "zzqx-code" / "zzqx-link.py")
    (eng / "zzqx-code" / "vendored" / ".git").mkdir(parents=True)
    (eng / "zzqx-code" / "vendored" / "x.py").write_text("zzqx\n", encoding="utf-8")
    for tree in (eng, dat):
        (tree / "zzqx-code").mkdir(exist_ok=True)
        (tree / "zzqx-code" / "both.py").write_text("zzqx\n", encoding="utf-8")
    got = _resolve_under(eng, dat, [
        ("zzqx-code/../../zzqx-data/zzqx-family/f.md", None),
        ("zzqx-code/zzqx-link.py", "engine"),
        (str(eng / "zzqx-code" / "zzqx-link.py"), None),
        ("zzqx-code/vendored/x.py", "engine"),
        ("/zzqx-elsewhere/zzqx-code/x.py", None),
        ("zzqx-code/both.py", None),
        ("zzqx-code/both.py", "engine"),
        ("zzqx-unlisted/gone.md", None),
        ("zzqx-code/gone.py", None),
        ("zzqx-code/./sub//new.py", None),
        ("zzqx-family/new.md", None),
        ("zzqx-code/../zzqx-code/main.py", "engine"),
    ])
    assert got == {
        "zzqx-code/../../zzqx-data/zzqx-family/f.md|None": "REFUSED:escape",
        "zzqx-code/zzqx-link.py|engine": "REFUSED:symlink-into-data",
        f"{eng / 'zzqx-code' / 'zzqx-link.py'}|None": "REFUSED:symlink-into-data",
        "zzqx-code/vendored/x.py|engine": "REFUSED:nested-repo",
        "/zzqx-elsewhere/zzqx-code/x.py|None": "REFUSED:outside",
        "zzqx-code/both.py|None": "REFUSED:ambiguous",
        "zzqx-code/both.py|engine": "REFUSED:data-in-engine",
        "zzqx-unlisted/gone.md|None": "REFUSED:unresolvable",
        "zzqx-code/gone.py|None": "engine",
        "zzqx-code/./sub//new.py|None": "engine",
        "zzqx-family/new.md|None": "private",
        "zzqx-code/../zzqx-code/main.py|engine": "engine",
    }


# ============================================================
# The reporting surface
# ============================================================

REPORT_REL = "scripts/classification-health.py"


def _git_tree(root: Path, files: dict[str, str], ignore: str | None = None) -> Path:
    """A committed git repository at `root` holding exactly `files`. Git, because
    the report asks `git check-ignore` what to skip and RAISES when git cannot."""
    root.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    if ignore is not None:
        (root / ".gitignore").write_text(ignore, encoding="utf-8")
    for argv in (["init", "-q"], ["add", "-A"],
                 ["-c", "user.email=zzqx@zzqx", "-c", "user.name=zzqx",
                  "commit", "-qm", "zzqx", "--allow-empty"]):
        subprocess.run(["git", "-C", str(root), *argv], check=True,
                       capture_output=True, timeout=120)
    return root


def _run_report(eng: Path, dat: Path | None, *argv) -> subprocess.CompletedProcess:
    """Drive the REAL report under scratch roots, as the operator runs it. `dat`
    of None leaves `HEADING_OS_DATA` unset, the public-clone and CI shape."""
    env = {"WORKSPACE_ROOT": str(eng), "PATH": "/usr/bin:/bin",
           "HOME": os.environ.get("HOME", str(eng))}
    if dat is not None:
        env["HEADING_OS_DATA"] = str(dat)
    return subprocess.run(
        [sys.executable, str(ROOT / REPORT_REL), *argv],
        cwd=str(ROOT), capture_output=True, text=True, timeout=300, env=env)


def _report_trees(tmp_path: Path) -> tuple[Path, Path]:
    """One scratch engine and one scratch overlay, both committed git repos.
    `zzqx-code/` EXISTS IN BOTH TREES on purpose, and `zzqx-junk/` is ignored in
    both, so a report that stopped asking git would count one file too many."""
    eng = _git_tree(
        tmp_path / "zzqx-engine",
        {"config/routing-map.yaml": INVENTED_ENGINE_MAP,
         "config/engine-allowlist.yaml": INVENTED_ALLOWLIST,
         "zzqx-code/a.py": "print('zzqx')\n",
         "zzqx-unruled/b.md": "zzqx\n",
         "zzqx-junk/ignored.txt": "zzqx\n"},
        ignore="zzqx-junk/\n")
    dat = _git_tree(
        tmp_path / "zzqx-data",
        {"zzqx-shared/c.md": "zzqx\n",
         "zzqx-private/d.md": "zzqx\n",
         "zzqx-code/e.py": "print('zzqx')\n",
         "zzqx-junk/ignored.txt": "zzqx\n"},
        ignore="zzqx-junk/\n")
    return eng, dat


def test_the_report_names_its_tree_and_answers_on_the_contract(tmp_path):
    """The report says WHICH tree each count is about, on the resolver's scale.

    MEASURED 2026-09-22 against an earlier version: one block of counts, no
    tree named, the engine printed under `Corporate`. Since HOS-232 an engine
    file outside the allowlist is counted under its REFUSAL, never `private`.
    `zzqx-code/` is allowlisted and exists in BOTH trees: the engine copy must
    come out `engine` and the overlay copy `private`, which is only possible if
    the report passes `tree=` rather than letting the resolver infer.
    """
    eng, dat = _report_trees(tmp_path)
    proc = _run_report(eng, dat, "--json")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    trees = json.loads(proc.stdout)["trees"]

    assert sorted(trees) == ["data", "engine"]
    assert trees["engine"]["root"] == str(eng)
    assert trees["data"]["root"] == str(dat)
    for block in trees.values():
        assert "ceo_only" not in block["counts"]

    assert trees["engine"]["files"]["engine"] == ["zzqx-code/a.py"]
    # The invented lists cover only `zzqx-code/`, so the scratch engine's own
    # two list files are outside them too, exactly like the unruled file.
    assert trees["engine"]["files"]["refused (boundary)"] == [
        "config/engine-allowlist.yaml", "config/routing-map.yaml",
        "zzqx-unruled/b.md"], trees["engine"]["files"]
    assert trees["engine"]["files"]["private"] == []
    assert "zzqx-code/e.py" in trees["data"]["files"]["private"], (
        "an allowlisted engine prefix reached into the overlay")
    assert "zzqx-shared/c.md" in trees["data"]["files"]["private"]
    assert trees["engine"]["total"] == 4 and trees["data"]["total"] == 3, (
        f"a git-ignored path was counted: {trees['engine']['total']} / "
        f"{trees['data']['total']}")


def test_a_report_that_walked_nothing_refuses_and_names_the_tree(tmp_path):
    """A walk that covered nothing REFUSES, and says which tree. The first case
    must still PASS, or a report hard-wired to refuse would satisfy the rest."""
    eng, dat = _report_trees(tmp_path)
    ok = _run_report(eng, dat)
    assert ok.returncode == 0, f"the honest case was refused:\n{ok.stderr}"

    empty = _git_tree(tmp_path / "zzqx-empty", {})
    refused = _run_report(eng, empty)
    assert refused.returncode == 1, "an empty overlay was reported as a pass"
    assert "data tree" in refused.stderr and str(empty) in refused.stderr, (
        f"the refusal named neither the tree nor the root: {refused.stderr}")

    no_overlay = _run_report(eng, None, "--tree", "data")
    assert no_overlay.returncode == 1
    assert "data" in no_overlay.stderr and "nothing was walked" in no_overlay.stderr

    degraded = _run_report(eng, None)
    assert degraded.returncode == 0, f"{degraded.stdout}\n{degraded.stderr}"
    assert "engine tree" in degraded.stdout
    assert "not walked: data" in degraded.stdout, (
        f"the absent overlay was not reported: {degraded.stdout}")

    blind = _git_tree(tmp_path / "zzqx-blind",
                      {"config/routing-map.yaml": INVENTED_ENGINE_MAP},
                      ignore="*\n")
    engine_empty = _run_report(blind, dat, "--tree", "engine")
    assert engine_empty.returncode == 1
    assert "engine tree" in engine_empty.stderr and str(blind) in engine_empty.stderr, (
        f"the engine-tree refusal named neither the tree nor the root: "
        f"{engine_empty.stderr}")
