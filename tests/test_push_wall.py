"""GUARANTEE: the push-time content scan refuses a planted credential, and neither of the two bypasses an operator can reach by hand gets one past it.

FAILURE A: a private byte reaching this public repository. This is the last wall
before the wire, so it is the one whose silence is unrecoverable.

HOW IT IS DRIVEN. `content_scan` in `scripts/push-all.py` is the function
`push_repo` calls before any push, loaded here by path and run over a THROWAWAY
git repository built per test under pytest's `tmp_path`. Its `origin/main` is
created with `git update-ref`, which is a local ref write: nothing in this file
contacts a network remote or writes to either real repository. The one
`git push` here, in the last test, goes to a bare repository under `tmp_path`. The
observable consequence asserted is the exit code the wall raises through
`sys.exit`, never the wording of its complaint.

THE TWO BYPASSES, which is why the repositories here carry history rather than
only a working tree. `.claude/rules/security.md` names `git commit --no-verify`
and a redirected `core.hooksPath` as the two gates a person can switch off by
hand, the second having once silently bypassed every hook. Both are exercised
against a secret that was committed and then WIPED FROM DISK, because that is
the shape in which a disarmed commit gate actually reaches a push: the file is
gone, the blob is not. The scan is unbypassable precisely because it does not
run as a git hook, so neither switch is in its path.

NOT CLAIMED. This covers the CONTENT wall over the push delta and the unpushed
history, and one question of the engine-cleanliness wall beside it: which
history paths it excuses as already published, and which history hits the
content wall excuses as already published in the same path (the next two
tests), and that each of push-all's six ratchets stops the push when its
scanner refuses (HOS-156), and that a forced push through `supervised_push` replaces
the remote's history only under a current lease, all leased refs or none
(HOS-59), and that a PUBLIC remote is refused unless the caller names
exactly that remote, the engine's own never (the last test). It says nothing
else about the routing walls, nothing about `scripts/publish-service.py`, which is a second
publication path with its own copy of this scan, and nothing about what happens
after the walls pass. It does not establish that `push_repo` calls `content_scan`
before pushing; it establishes only what `content_scan` does when called. No
fixture value was added to any allowlist: the planted credential is assembled at
run time from fragments, so no credential-shaped literal exists in this file.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# MEASURED 2026-09-21: scripts/utils/secret_patterns.py carries 17 patterns, each
# with a distinct description. The floor sits OUTSIDE every loop, because a loop
# over a pattern table that came back empty passes while checking nothing, and an
# emptied table is exactly what a weakened wall looks like.
PATTERN_FLOOR = 17

# The exit status `_refuse_on_scanner` raises. MEASURED 2026-09-21 on all three
# refusal cases below.
REFUSED = 2


def _planted_line() -> str:
    """One credential-shaped value, assembled here rather than written out.

    Deliberately not a literal. This workspace's own PreToolUse wall refused the
    writing of this file on 2026-09-21 when an earlier draft carried a token
    literal, and the wall was right: obligation 7 says change the fixture, never
    teach a gate that one particular thing looks private but is fine.
    """
    return "AWS_ACCESS_KEY_ID=" + "AKIA" + "ZZQX" + "3" * 12 + "\n"


@pytest.fixture(scope="module")
def push_all():
    """`scripts/push-all.py`, loaded by path because its name is not a module name.

    The interpreter is checked first. `push-all.py` calls `ensure_venv()` at
    import, which `os.execv`s the process under `.venv/bin/python` when it is run
    by any other interpreter; under pytest that would restart the whole run. A
    no-op is what is wanted here, and asserting it is cheaper than discovering it.
    """
    venv = ROOT / ".venv"
    # `sys.prefix`, not the resolved executable: `.venv/bin/python` is a symlink
    # onto the system interpreter, so resolving it lands outside the venv and the
    # check would fail against a correct interpreter. `ensure_venv` asks a
    # different question: it compares `interpreter_identity(sys.executable)`
    # with that of `.venv/bin/python`: the interpreter's resolved parent
    # directory paired with its resolved file. The two agree under a correct venv, and `conftest.py` sets the
    # relaunch sentinel so a disagreement still cannot re-exec pytest.
    if Path(sys.prefix) != venv:
        pytest.fail(
            f"this file must run under {venv}/bin/python; scripts/push-all.py "
            f"re-execs itself there at import and would restart pytest. "
            f"Current prefix: {sys.prefix}")
    target = ROOT / "scripts" / "push-all.py"
    if not target.is_file():
        pytest.fail(f"the push wall is not at {target}, so it cannot be driven")
    spec = importlib.util.spec_from_file_location("heading_push_all_under_test", target)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                          text=True, check=False, timeout=120)


def _throwaway_repo(tmp_path: Path, name: str) -> Path:
    """A repository with one honest commit already behind `origin/main`.

    `update-ref` rather than a push: the wall's history pass reads
    `origin/main..HEAD`, and that ref has to exist for there to be a delta at
    all, but nothing here may contact a remote. Writing the ref locally gives the
    same starting state with no network and no push.
    """
    repo = tmp_path / name
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "probe@example.invalid")
    _git(repo, "config", "user.name", "ZZQX probe")
    (repo / "README.md").write_text("ZZQX baseline\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "baseline")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    return repo


def _commit_then_wipe(repo: Path, *extra: str) -> None:
    """Plant the credential, commit it, then delete the file from the tree."""
    (repo / "creds.env").write_text(_planted_line(), encoding="utf-8")
    _git(repo, "add", "-A")
    assert _git(repo, "commit", "-q", "-m", "planted", *extra).returncode == 0
    (repo / "creds.env").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "wiped", *extra)
    assert not (repo / "creds.env").exists(), (
        "the fixture is meant to leave the credential in history and NOT on "
        "disk; a working-tree hit would let the weaker leg pass this test")


def _scan(push_all, repo: Path) -> int:
    """Drive the wall. Returns the exit status it raised, or 0 when it allowed."""
    try:
        push_all.content_scan(repo, will_commit=True)
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


def test_a_credential_in_the_push_delta_is_refused(push_all, tmp_path):
    """The working-tree leg: staged bytes about to become a commit."""
    repo = _throwaway_repo(tmp_path, "delta")
    (repo / "creds.env").write_text(_planted_line(), encoding="utf-8")
    _git(repo, "add", "-A")
    assert _scan(push_all, repo) == REFUSED


def test_a_credential_committed_with_no_verify_and_wiped_is_still_refused(
        push_all, tmp_path):
    """The first named bypass, with the commit gate skipped outright.

    MEASURED 2026-09-21: the file is absent from disk and the blob is carried by
    an unpushed commit, and the wall refuses on the history pass.
    """
    repo = _throwaway_repo(tmp_path, "noverify")
    _commit_then_wipe(repo, "--no-verify")
    assert _scan(push_all, repo) == REFUSED


def test_a_credential_committed_under_a_redirected_hooks_path_is_still_refused(
        push_all, tmp_path):
    """The second named bypass, and the one that once bypassed every hook.

    `core.hooksPath` is pointed at an empty directory, so the commit runs with no
    hook of any kind. The push wall is not a hook and does not notice.
    """
    repo = _throwaway_repo(tmp_path, "hookspath")
    empty = tmp_path / "no-hooks-here"
    empty.mkdir()
    _git(repo, "config", "core.hooksPath", str(empty))
    assert _git(repo, "config", "--get", "core.hooksPath").stdout.strip() == str(empty), (
        "the redirect did not take, so this test would pass against an ordinary "
        "commit and prove nothing about the bypass")
    _commit_then_wipe(repo)
    assert _scan(push_all, repo) == REFUSED


def test_an_honest_repository_still_passes(push_all, tmp_path):
    """The other direction, and it is not optional.

    A wall that refuses everything satisfies all three cases above and stops
    every backup. Without this, replacing `content_scan` with `sys.exit(2)`
    would leave this file green.
    """
    repo = _throwaway_repo(tmp_path, "honest")
    (repo / "notes.md").write_text("ZZQX ordinary content\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "honest")
    assert _scan(push_all, repo) == 0


def test_the_wall_names_a_scanner_that_exists_and_still_carries_its_patterns(push_all):
    """A wall pointed at nothing refuses nothing, whatever its code says.

    The pattern table is read from the gate at run time and never copied here:
    an enumeration of what a guard matches publishes, by the same act, what it
    does not, and this repository is public. Only the SIZE is asserted.
    """
    scanner = push_all.SCANNER
    if not Path(scanner).is_file():
        pytest.fail(f"scripts/push-all.py drives {scanner}, which does not exist, "
                    f"so every push would refuse or pass without reading anything")
    from scripts.utils.secret_patterns import SECRET_PATTERNS
    assert len(SECRET_PATTERNS) >= PATTERN_FLOOR, (
        f"the scanner carries {len(SECRET_PATTERNS)} patterns, below the "
        f"{PATTERN_FLOOR} measured 2026-09-21. The wall now refuses LESS than it "
        f"did, which is a weakening rather than a test to wave through.")


# Base tree for the published-engine case: a map whose `docs/` PREFIX says
# engine, and one file under it. The prefix is the trap: a wall trusting the
# base map alone would excuse every new path under `docs/` too.
_BASE_MAP = 'default: private\ndata_default: private\nrules:\n  "docs/": engine\n'


@pytest.mark.parametrize("case, expected", [
    # Edited in the range, then deleted: the remote holds it as engine and HEAD
    # no longer carries it. Refused before 2026-09-25, which is the defect.
    ("left-the-engine", 0),
    # Absent from the base tree, added then deleted inside the range. The base
    # map's prefix covers it; the base TREE does not, so it is judged by HEAD's.
    ("new-private-in-history", REFUSED),
    # Edited and still on disk: the working tree is judged by HEAD's map alone.
    ("still-on-disk", REFUSED),
])
def test_history_already_published_as_engine_is_excused_and_nothing_else(
        push_all, tmp_path, monkeypatch, case, expected):
    """FAILURE A, both directions of `published_engine_paths` through the real wall.

    Every `docs/zzqx-*` path is refused under this checkout's own lists,
    which is the one `engine_clean_scan` judges HEAD by, so the first case goes
    red against the wall as it was before 2026-09-25 and the other two go red if
    the excuse is widened to the base map alone or to files on disk.
    """
    monkeypatch.setattr(push_all, "log_denial", lambda **_: None)
    repo = tmp_path / case
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "probe@example.invalid")
    _git(repo, "config", "user.name", "ZZQX probe")
    (repo / "config").mkdir()
    (repo / "config" / "routing-map.yaml").write_text(_BASE_MAP, encoding="utf-8")
    (repo / "docs").mkdir()
    left = repo / "docs" / "zzqx-left.md"
    left.write_text("ZZQX published\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "baseline")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")

    target = left if case != "new-private-in-history" else repo / "docs" / "zzqx-new.md"
    target.write_text("ZZQX edited in the range\n", encoding="utf-8")
    _git(repo, "add", "-A")
    assert _git(repo, "commit", "-q", "-m", "edit").returncode == 0
    if case != "still-on-disk":
        # Both files leave, so nothing private is left on disk and a refusal
        # can only come from the history path under test.
        for gone in {target, left}:
            gone.unlink()
        _git(repo, "add", "-A")
        assert _git(repo, "commit", "-q", "-m", "leave").returncode == 0

    from scripts.utils.workspace import routing_verdict
    assert routing_verdict(target.relative_to(repo).as_posix()) != "engine", (
        "the fixture path must not route engine under HEAD's lists, or no case "
        "here tests the excuse at all")
    if case != "still-on-disk":
        assert not (repo / "docs").exists() or not any((repo / "docs").iterdir())
    try:
        push_all.engine_clean_scan(repo)
        status = 0
    except SystemExit as exc:
        status = int(exc.code or 0)
    assert status == expected
    if expected == 0:
        # The excused blob must still reach the content wall's selection.
        published = push_all.published_engine_paths(repo)
        assert push_all.engine_text_rels(["docs/zzqx-left.md"], published) == [
            "docs/zzqx-left.md"]


# The content-wall excuse below is driven with an INVENTED denylist: the wall's
# own `build_denylist` is swapped for one holding two made-up tokens, so no real
# entity reaches this public file. The probe path routes `engine` under this
# checkout's map, which the test asserts rather than assumes.
_PROBE = "scripts/zzqx_probe.py"
_OTHER = "scripts/zzqx_second.py"  # sorts after _PROBE: judged second
_OLD, _NEW = "zzqxpublishedtoken", "zzqxunpublishedtoken"


@pytest.mark.parametrize("case, expected", [
    # The base carries the token in this path; an unpushed version edits the
    # file and keeps it, a later commit drops it. Refused before 2026-09-25.
    ("already-published", 0),
    # A token the base version of the path never carried, added then removed.
    ("new-to-path", REFUSED),
    # The published token, but in a path whose base version never carried it.
    ("published-elsewhere", REFUSED),
    # The published token still on disk in the push delta: judged as always.
    ("still-on-disk", REFUSED),
])
def test_history_token_already_published_in_the_same_path_is_excused_and_nothing_else(
        push_all, tmp_path, monkeypatch, case, expected):
    """FAILURE A, both directions of the content wall's history excuse.

    The first case goes red against the wall as it was before 2026-09-25; the
    other three go red if the excuse is widened to any token of a published
    path, to a token published in any path, or to files on disk.
    """
    from scripts.utils.content_denylist import Denylist
    from scripts.utils.workspace import get_routing_destination

    for rel in (_PROBE, _OTHER):
        assert get_routing_destination(rel) == "engine", (
            f"{rel} must route engine, or the content wall never reads it")
    dl = Denylist(tokens={_OLD: "handle", _NEW: "handle"})
    dl._compile()
    monkeypatch.setattr(push_all, "build_denylist", lambda *_a, **_k: dl)
    monkeypatch.setattr(push_all, "log_denial", lambda **_: None)
    repo = _throwaway_repo(tmp_path, case)
    probe = repo / _PROBE
    probe.parent.mkdir()
    probe.write_text(f"ALLOW = ['{_OLD}']\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "published")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")

    target, token = {"already-published": (probe, _OLD),
                     "new-to-path": (probe, _NEW),
                     "published-elsewhere": (repo / _OTHER, _OLD),
                     "still-on-disk": (probe, _OLD)}[case]
    if case == "published-elsewhere":
        # The published path is also edited in the range, so its own excuse is
        # already computed when the second path is judged.
        probe.write_text(f"# edited\nALLOW = ['{_OLD}']\n", encoding="utf-8")
    target.write_text(f"# edited\nALLOW = ['{token}']\n", encoding="utf-8")
    _git(repo, "add", "-A")
    assert _git(repo, "commit", "-q", "-m", "range").returncode == 0
    if case != "still-on-disk":
        probe.write_text("ALLOW = []\n", encoding="utf-8")
        if target != probe:
            target.unlink()
        _git(repo, "add", "-A")
        assert _git(repo, "commit", "-q", "-m", "dropped").returncode == 0
        on_disk = "".join(p.read_text(encoding="utf-8")
                          for p in (repo / "scripts").iterdir())
        assert _OLD not in on_disk and _NEW not in on_disk, (
            "a token left on disk would let the disk sweep answer for history")
    try:
        push_all.engine_content_scan(repo, tmp_path, will_commit=True)
        status = 0
    except SystemExit as exc:
        status = int(exc.code or 0)
    assert status == expected


def _ratchet_ids():
    from scripts.utils.push_ratchets import RATCHETS
    assert len(RATCHETS) >= 6, "the ratchet table came back short; a loop over it would check nothing"
    return [r.name for r in RATCHETS]


@pytest.mark.parametrize("name", _ratchet_ids())
@pytest.mark.parametrize("scanner_exit, expected", [(1, REFUSED), (0, 0)])
def test_every_push_ratchet_stops_the_push_when_its_scanner_refuses(
        push_all, monkeypatch, name, scanner_exit, expected):
    """FAILURE A: a ratchet that PRINTS its refusal must also STOP the push.

    HOS-156, MEASURED 2026-09-28: `outside_workspace_writes_ratchet` printed
    "REFUSING TO PUSH" and returned, and push-all pushed the engine anyway. The
    `engine-only` ratchet in this table is the one that keeps a non-engine file
    out of this public repository. The scanner is replaced by its verdict, so
    no scanner runs; the exit code is what is asserted, never the wording. The
    refusing half goes red against the code before HOS-156 for
    `outside-workspace-writes`; the passing half goes red if any ratchet
    refuses a clean scan.
    """
    from scripts.utils.push_ratchets import RATCHETS
    ratchet = next(r for r in RATCHETS if r.name == name)
    gate = getattr(push_all, name.replace("-", "_") + "_ratchet", None)
    if gate is None:
        pytest.fail(f"ratchet {name} is in the table but push-all has no function for it")
    monkeypatch.setattr(push_all, "run_ratchet", lambda _repo, r: (
        subprocess.CompletedProcess([r.script], scanner_exit, "ZZQX\n", "")
        if r is ratchet else pytest.fail(f"{name} ran {r.name}")))
    try:
        gate(ROOT)
        status = 0
    except SystemExit as exc:
        status = int(exc.code or 0)
    assert status == expected


def _published_then_rewritten(tmp_path: Path) -> tuple[Path, Path, dict]:
    """A repository whose main and annotated tag were pushed, then rewritten.

    The remote is a bare repository under `tmp_path`; nothing leaves the
    machine. The rewrite keeps the tree and replaces the commit, which is the
    shape a history cleanup leaves: every id differs, nothing on disk does.
    Returns (work, remote, the ids the remote holds before the forced push).
    """
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "probe@example.invalid")
    _git(work, "config", "user.name", "ZZQX probe")
    (work / "README.md").write_text("ZZQX baseline\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "baseline")
    _git(work, "tag", "-a", "zzqx-v0", "-m", "ZZQX tag")
    _git(work, "remote", "add", "origin", str(remote))
    assert _git(work, "push", "-q", "origin", "main", "refs/tags/zzqx-v0").returncode == 0
    before = {ref: _git(remote, "rev-parse", ref).stdout.strip()
              for ref in ("refs/heads/main", "refs/tags/zzqx-v0")}
    new = _git(work, "commit-tree", "HEAD^{tree}", "-m", "rewritten").stdout.strip()
    _git(work, "update-ref", "refs/heads/main", new)
    _git(work, "tag", "-f", "-a", "zzqx-v0", "-m", "ZZQX tag", new)
    return work, remote, before


@pytest.mark.parametrize("lease", ["current", "stale", "missing-id"])
def test_a_forced_push_replaces_only_the_history_the_caller_saw(tmp_path, lease):
    """HOS-59: `force_with_lease` rewrites the remote, and only under a current lease.

    Both directions, through `supervised_push` itself against a local bare
    remote, asserting the ids the remote holds afterwards. "current" replaces
    main and the tag and is verified; "stale" (the remote holds something else
    than the caller saw) and "missing-id" (no explicit expected id) must leave
    the remote exactly as it was. Red against the code before 2026-10-04, which
    had no such parameter and could push one branch fast-forward only.
    """
    from scripts.utils.git_push import supervised_push

    work, remote, before = _published_then_rewritten(tmp_path)
    leases = {"current": dict(before),
              "stale": {**before, "refs/heads/main": "0" * 39 + "1"},
              "missing-id": {**before, "refs/heads/main": ""}}[lease]
    verdict = supervised_push(work, stall_window=60, log_dir=str(tmp_path),
                              force_with_lease=leases)
    after = {ref: _git(remote, "rev-parse", ref).stdout.strip() for ref in before}
    if lease == "current":
        assert verdict["state"] == "ok", verdict
        assert after == {ref: _git(work, "rev-parse", ref).stdout.strip() for ref in before}
        assert after != before
    else:
        assert verdict["state"] != "ok", verdict
        assert after == before


def _pushable_pair(tmp_path: Path, name: str) -> tuple[Path, Path]:
    """A work repository with one pushed baseline commit and its bare remote.

    `origin/main` is real here, because the pre-push scan reads
    `origin/main..HEAD` exactly as `push_repo` does. Nothing leaves `tmp_path`.
    """
    remote = tmp_path / f"{name}-remote.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    work = _throwaway_repo(tmp_path, name)
    _git(work, "remote", "add", "origin", str(remote))
    _git(work, "update-ref", "-d", "refs/remotes/origin/main")
    assert _git(work, "push", "-q", "--no-verify", "origin", "main").returncode == 0
    return work, remote


def _remote_main(remote: Path) -> str:
    return _git(remote, "rev-parse", "--verify", "-q", "refs/heads/main").stdout.strip()


def test_a_plain_git_push_of_the_data_repository_runs_the_content_scan(tmp_path):
    """A-1.1: a push that never entered push-all.py still meets the content wall.

    The versioned data hook is installed as `install-git-hooks.py` installs it,
    with this checkout stamped in as the engine, and driven by a real
    `git push` to a bare remote. A credential committed and wiped is refused and
    the remote does not move; an honest commit is pushed. MEASURED 2026-10-16
    against the hook before the fix: the planted commit reached the remote,
    because the hook ran the overlay's tests and git-lfs and nothing else.
    """
    hook_src = (ROOT / ".githooks" / "pre-push-data").read_text(encoding="utf-8")
    hook_body = hook_src.replace("@@ENGINE_ROOT@@", str(ROOT))

    def arm(work: Path) -> None:
        hook = Path(_git(work, "rev-parse", "--git-path", "hooks").stdout.strip())
        hook = hook if hook.is_absolute() else work / hook
        hook.mkdir(parents=True, exist_ok=True)
        (hook / "pre-push").write_text(hook_body, encoding="utf-8")
        (hook / "pre-push").chmod(0o755)

    planted, planted_remote = _pushable_pair(tmp_path, "plain-planted")
    arm(planted)
    before = _remote_main(planted_remote)
    _commit_then_wipe(planted, "--no-verify")
    refused = _git(planted, "push", "-q", "origin", "main")
    assert refused.returncode != 0, refused.stdout + refused.stderr
    assert _remote_main(planted_remote) == before

    honest, honest_remote = _pushable_pair(tmp_path, "plain-honest")
    arm(honest)
    (honest / "notes.md").write_text("ZZQX ordinary content\n", encoding="utf-8")
    _git(honest, "add", "-A")
    _git(honest, "commit", "-q", "-m", "honest")
    allowed = _git(honest, "push", "-q", "origin", "main")
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr
    assert _remote_main(honest_remote) == _git(honest, "rev-parse", "HEAD").stdout.strip()


def test_the_engine_hook_runs_the_content_scan_before_the_suite(tmp_path):
    """A-1.1, the engine half: the versioned engine hook hands git's ref lines
    to `push-all.py --pre-push-scan engine` first, and a refusal there stops the
    push before the suite or git-lfs runs.

    Driven by a real `git push` with the real hook text. The two scripts it
    calls are stand-ins in the throwaway repository, because the hook resolves
    them under its own checkout: the scan stand-in records what it was handed
    and refuses, the suite stand-in records that it ran. Before the fix the hook
    never called the scan, so the push reached the remote.
    """
    work, remote = _pushable_pair(tmp_path, "engine-hook")
    record = tmp_path / "scan-call.txt"
    suite = tmp_path / "suite-ran.txt"
    scripts = work / "scripts"
    scripts.mkdir()
    (scripts / "push-all.py").write_text(
        "import sys\n"
        f"open({str(record)!r}, 'w').write(' '.join(sys.argv[1:]) + '\\n' + sys.stdin.read())\n"
        "sys.exit(2)\n", encoding="utf-8")
    (scripts / "run-tests.py").write_text(
        f"open({str(suite)!r}, 'w').write('ran')\n", encoding="utf-8")
    hook = Path(_git(work, "rev-parse", "--git-path", "hooks").stdout.strip())
    hook = hook if hook.is_absolute() else work / hook
    hook.mkdir(parents=True, exist_ok=True)
    (hook / "pre-push").write_text(
        (ROOT / ".githooks" / "pre-push").read_text(encoding="utf-8"), encoding="utf-8")
    (hook / "pre-push").chmod(0o755)
    before = _remote_main(remote)
    (work / "notes.md").write_text("ZZQX ordinary content\n", encoding="utf-8")
    _git(work, "add", "notes.md")
    _git(work, "commit", "-q", "-m", "next")
    head = _git(work, "rev-parse", "HEAD").stdout.strip()

    pushed = _git(work, "push", "-q", "origin", "main")
    assert pushed.returncode != 0, pushed.stdout + pushed.stderr
    assert _remote_main(remote) == before
    assert record.is_file(), "the engine hook never called the content scan"
    argv, _, stdin = record.read_text(encoding="utf-8").partition("\n")
    assert argv.split()[:2] == ["--pre-push-scan", "engine"]
    assert head in stdin, "the scan was not handed git's ref lines"
    assert not suite.exists(), "the suite ran although the scan refused"


def test_the_pre_push_scan_refuses_a_ref_it_did_not_scan(push_all, tmp_path):
    """The scan reads `origin/main..HEAD`, so a pushed commit that is not HEAD
    is refused, and push-all's marker excuses only the commit it names."""
    repo = _throwaway_repo(tmp_path, "other-ref")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    other = _git(repo, "commit-tree", "HEAD^{tree}", "-m", "elsewhere").stdout.strip()
    zero = "0" * 40

    def scan(sha: str, env: dict) -> int:
        try:
            push_all.pre_push_scan("data", repo,
                                   f"refs/heads/x {sha} refs/heads/x {zero}\n", env)
        except SystemExit as exc:
            return int(exc.code or 0)
        return 0

    assert scan(other, {}) == REFUSED
    assert scan(other, {push_all.PUSH_SCANNED_ENV: head}) == REFUSED
    assert scan(other, {push_all.PUSH_SCANNED_ENV: other}) == 0
    assert scan(head, {}) == 0


def test_public_remote_refused_unless_it_is_the_named_official_remote(tmp_path, monkeypatch):
    """A PUBLIC remote is refused unless the caller names exactly that remote; the engine's never.

    `publish` of HEADING OS Official must reach its two remotes once they are
    public, and nothing else may gain that. Driven through `remote_objection`
    over throwaway repositories under `tmp_path`; GitHub's visibility answer is
    replaced by "public" and the workspace roots by invented ones, so no network
    and no real repository is reached. Red before 2026-10-28: the "equal" case
    raised TypeError, the parameter did not exist (MEASURED 2026-10-28).
    """
    from scripts.utils import git_push

    official = "https://github.com/zzqx-org/zzqx-official.git"  # invented
    engine_url = "https://github.com/zzqx-org/zzqx-engine.git"   # invented
    engine, data = tmp_path / "engine", tmp_path / "data"
    for repo, url in ((engine, engine_url), (tmp_path / "work", official)):
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "remote", "add", "origin", url)
    data.mkdir()
    monkeypatch.setattr(git_push, "get_workspace_root", lambda: engine)
    monkeypatch.setattr(git_push, "get_data_root", lambda: data)
    monkeypatch.setattr(git_push, "_gh_visibility", lambda normalized, token=None: "public")
    monkeypatch.setattr(git_push, "_VIS_CACHE", {})
    work = tmp_path / "work"

    assert "PUBLIC" in (git_push.remote_objection(work, token="x") or "")
    assert "PUBLIC" in (git_push.remote_objection(
        work, token="x", public_ok="https://github.com/zzqx-org/zzqx-other.git") or "")
    assert git_push.remote_objection(work, token="x", public_ok=official) is None
    _git(work, "remote", "set-url", "origin", engine_url)
    assert "ENGINE remote" in (git_push.remote_objection(work, token="x", public_ok=engine_url) or "")
