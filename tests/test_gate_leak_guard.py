"""GUARANTEE: engine code cannot carry a hardcoded path into the operator's private data, and a path that routes private cannot be staged into this repository.

FAILURE A, a private byte reaching this public repository. The two halves are
the two subcommands of one gate. `check-paths` reads engine SOURCE and refuses a
string literal that opens with a data-overlay token, which is how an address
inside the private tree gets published without any private file moving. And
`check-staged` reads a list of PATHS and refuses one that does not route
`engine`, which is how the file itself gets published.

HOW THIS DRIVES THE GATE. Through `subprocess`, at `scripts/leak-guard.py`,
which is byte for byte the command a commit reaches: `.pre-commit-config.yaml`
registers both subcommands as local hooks whose `entry` is that same checkout
path under `.venv/bin/python`, so there is no installed second copy to read by
mistake. Nothing here restates the gate's condition, and nothing here holds a
copy of its token list: the list is read out of the running gate, in its own
interpreter, and looped over. A copy would drift, and a copy in a public
repository would also publish which addresses the gate does NOT refuse.

THE FIXTURES ARE PLANTED INSIDE THIS CHECKOUT, briefly, and removed in a
`finally`. The first draft put them in `tmp_path` and every one of the 14 came
back a survivor. MEASURED 2026-09-21: `tests/conftest.py` points
`HEADING_OS_DATA` at the test's own `tmp_path` for the whole session, so a
fixture written there sits UNDER the data root, the routing map answers
`private` for it, and `check_paths` skips every path that does not route
`engine`. The gate was behaving correctly and the fixture was never scanned. A
file inside the checkout is also what a commit actually looks like, so that is
where they go; nothing is left behind, and each name carries the process id so
two runs cannot collide.

THE RUNTIME STORE, added 2026-10-13 (HOS-241). Its base is a registry value,
`runtime_store:`, which the C3 move changes from the default to a nested
`system/runtime`. Two guards must answer for BOTH bases while that happens:
`check-paths` refuses a literal under either, and
`.claude/hooks/data-path-redirect.py` sends a relative write under either to
the data root rather than into this checkout. Both are driven over a synthetic
data root whose registry moves the store, and both were red against `main`.

NOT CLAIMED. This says nothing about the push path, nothing about a literal
already committed, nothing about the AST half of the gate that refuses a front
door call whose address is not a literal, and nothing about the gate's
unreadable-file and vanished-file branches. It also says nothing about whether
the hooks are ARMED in a given clone; `scripts/setup-platform.sh --check` and
`scripts/check-gate-integrity.py` own that, and the last test below asserts only
that the two subcommands are named in the config at all.
"""
from __future__ import annotations

import contextlib
import itertools
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / "scripts" / "leak-guard.py"
CONFIG = ROOT / ".pre-commit-config.yaml"

# MEASURED 2026-09-21 against this tree: the gate refuses 14 data-path tokens,
# five legacy overlay roots plus the nine HEADING OS 2 family roots it derives
# from `families.family_roots()`. The floor is that measured number, asserted
# OUTSIDE every loop, because a loop over a list that came back empty passes
# while checking nothing. A number BELOW it means the gate refuses less than it
# did, which is a weakening and not a test to wave through.
TOKEN_FLOOR = 14

# Invented, and carrying the workspace's grep sentinel so no reader can mistake
# any of it for a real address.
SENTINEL = "zzqx-probe"


def _ask_the_gate_for_its_tokens() -> list[str]:
    """The token list the RUNNING gate refuses on, read from the gate itself.

    In a child interpreter rather than in this one. The gate's module body
    mutates `sys.path` and imports the family map, and the suite arms a guard
    that fails a test which leaves `sys.path` changed, so reading the list in
    process would make this file fail for a reason that has nothing to do with
    the guarantee. The child is started from `sys.executable`, which is the
    pinned interpreter the hook entry also names.
    """
    program = (
        "import importlib.util, json, sys;"
        "spec = importlib.util.spec_from_file_location('leak_guard', sys.argv[1]);"
        "mod = importlib.util.module_from_spec(spec);"
        "spec.loader.exec_module(mod);"
        "print(json.dumps(list(mod.DATA_PATH_TOKENS)))"
    )
    proc = subprocess.run([sys.executable, "-c", program, str(GATE)],
                          cwd=str(ROOT), capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        pytest.fail(f"the gate at {GATE} could not be loaded, so its token list "
                    f"could not be read and nothing below tests anything:\n"
                    f"{proc.stdout}\n{proc.stderr}")
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        pytest.fail(f"the gate loaded but published no DATA_PATH_TOKENS list "
                    f"({exc}); its shape has changed and this file is now blind "
                    f"to it:\n{proc.stdout}\n{proc.stderr}")


_counter = itertools.count()


@contextlib.contextmanager
def _planted(body: str):
    """One engine-routed `.py` inside the checkout, gone before the test ends.

    `scripts/` rather than `tests/`, because `check_paths` skips anything whose
    repo-relative path opens with `tests/`: test files legitimately embed
    data-path literals as fixtures, so a fixture planted there is never read.
    """
    target = ROOT / "scripts" / f"zzqx_leak_probe_{os.getpid()}_{next(_counter)}.py"
    try:
        target.write_text(body, encoding="utf-8")
        yield target
    finally:
        target.unlink(missing_ok=True)


def _run(subcommand: str, *files: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """The REAL entry point a commit reaches, not an imported helper."""
    return subprocess.run(
        [sys.executable, str(GATE), subcommand, "--files", *files],
        cwd=str(ROOT), capture_output=True, text=True, timeout=300, env=env,
    )


@pytest.fixture(scope="module")
def tokens() -> list[str]:
    found = _ask_the_gate_for_its_tokens()
    assert len(found) >= TOKEN_FLOOR, (
        f"the gate lists {len(found)} data-path tokens, below the {TOKEN_FLOOR} "
        f"measured 2026-09-21. Every loop below iterates this list, so a shorter "
        f"list is a quieter gate wearing a green suite.")
    return found


def test_engine_code_holding_any_token_the_gate_lists_is_refused(tokens):
    """One subprocess per token, each planting a literal that opens with it.

    The assertion is the EXIT CODE. A message is prose the gate may reword; the
    exit code is what stops the commit.
    """
    survivors: list[str] = []
    for token in tokens:
        prefix = token if token.endswith("/") else token + "/"
        with _planted(f'PATH = "{prefix}{SENTINEL}"\n') as target:
            if _run("check-paths", str(target)).returncode == 0:
                survivors.append(token)
    assert not survivors, (
        "the gate passed engine code holding a literal that opens with a token "
        "it lists as a data-overlay address: " + ", ".join(survivors))


def test_engine_code_with_no_data_path_literal_still_passes():
    """The other direction, and it is not optional.

    A gate that refuses everything satisfies the test above and breaks every
    commit. Without this, replacing `check_paths` with `return 1` leaves this
    file green. Planted in the same place and the same way as the refused case,
    so the two differ in their CONTENT and in nothing else.
    """
    with _planted(f'VALUE = "{SENTINEL} ordinary engine content"\n') as target:
        result = _run("check-paths", str(target))
    assert result.returncode == 0, (
        "the gate refused engine code carrying no data-path literal:\n"
        f"{result.stdout}\n{result.stderr}")


def _store_moved_root(tmp_path: Path) -> tuple[dict, str, str]:
    """(env, default base, registry base) over a synthetic data root whose registry moves the store.

    The default comes from the ENGINE's default registry and the moved name
    from the registry written here, never from either gate under test, which
    is what makes the two tests below red against a gate that does not read
    them.
    """
    data = tmp_path / "data"
    built = subprocess.run([sys.executable, str(ROOT / "scripts" / "data-path.py"),
                            "synthetic", str(data)],
                           cwd=str(ROOT), capture_output=True, text=True, timeout=300)
    assert built.returncode == 0, f"{built.stdout}\n{built.stderr}"
    default = next(line.split(":", 1)[1].strip() for line in
                   (ROOT / "scripts" / "data-names.example.yaml").read_text(
                       encoding="utf-8").splitlines()
                   if line.startswith("runtime_store:"))
    moved = f"{SENTINEL}-store"
    registry = data / "config" / "data-paths.yaml"
    text = registry.read_text(encoding="utf-8")
    assert "version: 1\n" in text, "the synthetic registry changed shape"
    registry.write_text(text.replace("version: 1\n", f"version: 1\nruntime_store: {moved}\n", 1),
                        encoding="utf-8")
    return {**os.environ, "HEADING_OS_DATA": str(data)}, default, moved


def test_a_literal_under_the_runtime_store_is_refused_under_either_base(tmp_path):
    """The runtime store's address, under the default base AND the registry's.

    HOS-241, 2026-10-13. The store holds browser cookies, drafted mail and
    calendar dumps under the data root, and its name was in no token list, so
    `"<store>/..."` passed this gate. MEASURED that day on a synthetic root
    whose registry moves the store: the gate on `main` exited 0 for both
    literals below. The honest half is a URL carrying the same name
    mid-string, which must still pass.
    """
    env, default, moved = _store_moved_root(tmp_path)
    survivors = []
    for base in (default, moved):
        with _planted(f'PATH = "{base}/browser/cache/{SENTINEL}.json"\n') as target:
            if _run("check-paths", str(target), env=env).returncode == 0:
                survivors.append(base)
    assert not survivors, "the gate passed a literal under the runtime store: " + ", ".join(survivors)
    with _planted(f'URL = "https://example.invalid/{moved}/{SENTINEL}"\n') as target:
        honest = _run("check-paths", str(target), env=env)
    assert honest.returncode == 0, f"{honest.stdout}\n{honest.stderr}"


def test_a_relative_write_under_the_runtime_store_lands_in_the_data_root(tmp_path):
    """`.claude/hooks/data-path-redirect.py`: a relative write spelled under either
    store base is rewritten into the data root, and an engine path is left alone.

    HOS-241, 2026-10-13: on `main` the hook printed nothing for either base, so
    the write went ahead against the ENGINE checkout. Driven as the harness
    drives it, one PreToolUse payload on stdin per case.
    """
    env, default, moved = _store_moved_root(tmp_path)
    hook = ROOT / ".claude" / "hooks" / "data-path-redirect.py"

    def redirected(rel: str) -> str:
        payload = json.dumps({"tool_name": "Write",
                              "tool_input": {"file_path": rel, "content": SENTINEL}})
        out = subprocess.run([sys.executable, str(hook)], input=payload, cwd=str(ROOT),
                             capture_output=True, text=True, timeout=300, env=env).stdout
        return json.loads(out)["hookSpecificOutput"]["updatedInput"]["file_path"] if out.strip() else ""

    data = Path(env["HEADING_OS_DATA"]).resolve()
    for base in (default, moved):
        rel = f"{base}/{SENTINEL}/x.json"
        assert redirected(rel) == str(data / rel), f"a write under {base}/ was not sent to the data root"
    assert redirected(f"scripts/{SENTINEL}.py") == "", "an engine path was redirected"


def test_a_staged_path_under_any_token_the_gate_lists_is_refused(tokens):
    """The second subcommand, over the same list, one subprocess per token.

    `check-staged` reads PATHS and never opens them, so nothing is written
    anywhere for this loop. MEASURED 2026-09-21: all 14 tokens route `private`
    and all 14 are refused.
    """
    survivors: list[str] = []
    for token in tokens:
        prefix = token if token.endswith("/") else token + "/"
        if _run("check-staged", f"{prefix}{SENTINEL}.md").returncode == 0:
            survivors.append(token)
    assert not survivors, (
        "the gate let a path under a private data-overlay root be staged into "
        "this public repository: " + ", ".join(survivors))


def test_ordinary_engine_paths_are_still_allowed_to_be_staged():
    """The honest direction for the second subcommand.

    Real tracked engine files, because a path that does not exist would prove
    nothing about a classifier that answers from the path alone. An absent one
    fails here rather than being skipped.
    """
    honest = ["README.md", "docs/INSTALL.md", "scripts/leak-guard.py"]
    missing = [rel for rel in honest if not (ROOT / rel).is_file()]
    if missing:
        pytest.fail(f"these engine files were expected on disk and are not "
                    f"there, so the honest direction was never driven: {missing}")
    result = _run("check-staged", *honest)
    assert result.returncode == 0, (
        "the gate refused ordinary engine files being staged into the engine "
        f"repository:\n{result.stdout}\n{result.stderr}")


def test_both_subcommands_are_registered_in_the_config_that_arms_them(tokens):
    """A gate nothing invokes refuses nothing, whatever its code says.

    This asserts registration only. Whether the registration's `files:` pattern
    selects anything is `scripts/check-gate-integrity.py`'s, and it already runs
    on every commit.
    """
    config = CONFIG.read_text(encoding="utf-8")
    for subcommand in ("check-paths", "check-staged"):
        assert f"leak-guard.py {subcommand}" in config, (
            f"leak-guard.py {subcommand} is not registered in "
            f"{CONFIG.name}, so nothing runs it at commit time and the cases "
            f"above prove only that the subcommand exists on disk")


def test_an_engine_tree_whose_data_root_collapses_onto_it_still_refuses(tmp_path):
    """`check-staged` stays armed when the data root resolves to the engine itself.

    The gate decided it was in the engine by comparing the data root with the
    workspace root. Two ordinary states made them equal: a `knowledge/` (or
    `crm/contacts/`) directory inside the engine, which `get_data_root()` took
    for legacy in-tree data, and a `HEADING_OS_DATA` naming the engine checkout.
    Either one turned the gate inert and every private path was staged freely.
    MEASURED 2026-09-28 on an extracted copy of the pre-fix tree with a
    `knowledge/` directory inside it: `check-staged --files
    crm/contacts/<probe>.md` exited 0; without the directory it exited 1.

    The engine here is a scratch tree carrying the engine's own marker and map,
    pointed at by `WORKSPACE_ROOT`, so nothing is planted in this checkout and no
    refusal is logged into a live data root. The honest direction is the test
    above that stages real engine files.
    """
    engine = tmp_path / "engine"
    (engine / ".claude").mkdir(parents=True)
    (engine / "CLAUDE.md").write_text("probe\n", encoding="utf-8")
    (engine / "config").mkdir()
    for rel in ("config/engine-allowlist.yaml", "config/routing-map.yaml"):
        (engine / rel).write_bytes((ROOT / rel).read_bytes())
    (engine / "knowledge").mkdir()

    staged = f"crm/contacts/{SENTINEL}.md"
    base = {k: v for k, v in os.environ.items()
            if k not in ("HEADING_OS_DATA", "HEADING_OS_ENGINE_REPO")}
    base["WORKSPACE_ROOT"] = str(engine)
    cases = {
        "a knowledge/ directory inside the engine": base,
        "HEADING_OS_DATA naming the engine": {**base, "HEADING_OS_DATA": str(engine)},
    }
    passed = []
    for label, env in cases.items():
        proc = subprocess.run(
            [sys.executable, str(GATE), "check-staged", "--files", staged],
            cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=300)
        if proc.returncode == 0:
            passed.append(label)
    assert not passed, (
        "check-staged let a private path into the engine repository when the "
        "data root resolved to the engine tree: " + "; ".join(passed))


# ---------------------------------------------------------------------------
# The home-path half of the same guarantee, added 2026-09-21.
#
# `scripts/check-home-paths.py` refuses a real home directory in this public
# repository, which is the same failure leak-guard covers from the other side:
# engine code must not carry an address into the operator's private world. A
# home path publishes the account name, the machine's layout and the names of
# its working trees.
#
# WHY IT IS HERE AND NOT IN A FILE OF ITS OWN. One test file per GUARANTEE, and
# the guarantee is the one this file already holds. It arrived as the first of
# twelve gates frozen in `scripts/check-gate-integrity.py`'s baseline on
# 2026-09-21, and it was the one entry in that list called out as uncomfortable
# to freeze, because unlike the other eleven its subject IS disclosure. It stops
# being frozen here.
# ---------------------------------------------------------------------------

HOME_GATE = "scripts/check-home-paths.py"

# MEASURED 2026-09-21 by running the gate: three home roots are recognised
# (`/home/`, `/users/`, a Windows `C:\users\`) and the placeholder vocabulary
# holds 18 account names. Both floors are asserted OUTSIDE the loops below,
# because a vocabulary that came back empty would make every planted case a
# finding and every honest case a false alarm, and a loop over an empty root
# list would check nothing while passing.
#
# The first version of this line said 21, which was a number nobody had taken.
# The assertion went red on its own author within the minute, which is the
# shape a floor is supposed to have: it is measured, or it is decoration.
HOME_ROOT_FLOOR = 3
PLACEHOLDER_FLOOR = 18


def _home_gate():
    """The gate itself, imported, so its rule is READ rather than restated.

    The module's own comment says the rule is kept pure "so it can be exercised
    on synthetic source", and that is the seam used here. Its `sys.path` effect
    is undone, because this suite fails a test that leaves `sys.path` changed.
    """
    import importlib.util
    path = ROOT / HOME_GATE
    if not path.is_file():
        pytest.fail(f"{HOME_GATE} does not exist, so the gate this covers is "
                    f"not in this repository at all")
    spec = importlib.util.spec_from_file_location("_hos54_home_paths", path)
    module = importlib.util.module_from_spec(spec)
    saved = sys.path[:]
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = saved
    return module


@pytest.fixture(scope="module")
def home_gate():
    gate = _home_gate()
    assert len(gate.PLACEHOLDER_ACCOUNTS) >= PLACEHOLDER_FLOOR, (
        f"the gate knows {len(gate.PLACEHOLDER_ACCOUNTS)} placeholder accounts, "
        f"below the {PLACEHOLDER_FLOOR} measured 2026-09-21. It now refuses "
        f"invented examples, which is how a guard gets switched off.")
    return gate


def test_this_repository_carries_no_real_home_directory(home_gate):
    """The honest direction, driven through the REAL command over the REAL tree.

    This is the state the commit gate is in right now, and it is the half that
    matters most: a gate that refuses this repository as it stands would be
    turned off within the day.
    """
    result = subprocess.run(
        [sys.executable, str(ROOT / HOME_GATE), "--check"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, (
        f"the home-path gate refuses this repository as it stands:\n"
        f"{result.stdout}\n{result.stderr}")


def test_every_home_root_the_gate_knows_is_refused_with_a_real_account(home_gate):
    """The refusal direction, one case per root, over synthetic source.

    The roots are not listed here. They are read off the gate's own compiled
    pattern, so a fourth root added tomorrow gets a case tomorrow with nobody
    editing this file, and nothing in this public repository restates what the
    gate looks for.
    """
    roots = [r for r in ("/home/", "/users/", "C:\\users\\")
             if home_gate.HOME_RE.search(r + "zzqxaccount/work")]
    assert len(roots) >= HOME_ROOT_FLOOR, (
        f"only {len(roots)} of the home roots are recognised, below the "
        f"{HOME_ROOT_FLOOR} measured 2026-09-21; the pattern has narrowed and "
        f"the cases below cover less than they did")

    survivors = [
        root for root in roots
        if not home_gate.findings_in("zzqx.md", f"see {root}zzqxaccount/work\n")
    ]
    assert not survivors, (
        "the gate admitted a real account name under these home roots: "
        + ", ".join(survivors))


def test_an_invented_account_and_a_declared_reason_both_still_pass(home_gate):
    """Two honest cases, because a gate refusing both is a gate people disable.

    An invented account is how the documentation is supposed to be written, and
    a declared reason is the escape the gate itself offers for a real path that
    genuinely has to stay. Neither may be a finding.
    """
    invented = [account for account in sorted(home_gate.PLACEHOLDER_ACCOUNTS)
                if home_gate.findings_in("zzqx.md", f"see /home/{account}/work\n")]
    assert not invented, (
        "the gate reported an account it lists as a placeholder, so the "
        "documentation it asks people to write would not pass it: "
        + ", ".join(invented))

    excused = home_gate.findings_in(
        "zzqx.md",
        "# home-path: ok (a ZZQX reason long enough for a reviewer to read it)\n"
        "see /home/zzqxaccount/work\n")
    assert not excused, (
        "the gate reported a path carrying its own declared reason, so the "
        "one escape it documents does not work and the next person to meet it "
        "will reach for the allowlist instead")
