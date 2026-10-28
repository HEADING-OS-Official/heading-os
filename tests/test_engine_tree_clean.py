#!/usr/bin/env python3
"""Runtime proof: the engine clone (.heading-os) carries NO data-class artifact.

HEADING OS engine/data separation invariant: the engine repo is code only; every
data artifact lives in the DATA root (.heading-os-data) reached via the get_*_dir()
seam. The static guard (test_data_root_no_bypass.py) proves *code* does not join a
data dir to an engine root. THIS test proves the complementary, runtime fact: the
engine working tree itself contains no file that routes private or is refused.

Why both: finding #3 (2026-06-16) showed a static regex guard can miss an entire
misroute class for years. A tree-level assertion is the belt to the regex's braces
-- if anything ever lands a data artifact in the engine clone (a script, a SKILL
Bash call, or a plugin write), this fails regardless of how the write happened.

The detector itself lives in scripts/utils/engine_guard.py so the UNBYPASSABLE
push wall (scripts/push-all.py) enforces the exact same invariant this asserts --
the 2026-06-22 `docs/superpowers/` leak survived because the routing check ran
only at layers `--no-verify` skips, so the logic is now shared, not test-only.

Filtering is by routing destination, NOT a raw top-level-name match: classification
carve-outs (e.g. `datastore/brand/templates/` routes ENGINE) legitimately share a
top-level name with data dirs and must NOT be flagged.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pytest  # noqa: E402

from scripts.utils.engine_guard import (  # noqa: E402
    DEMO_MANIFEST,
    find_data_artifacts,
    repo_carried_paths,
    scan_engine_repo,
)
from scripts.utils.workspace import get_workspace_root  # noqa: E402

# --- Negative branch: the real engine tree must be clean ---------------------


def test_engine_tree_has_no_data_artifacts():
    flagged = scan_engine_repo(get_workspace_root())
    assert not flagged, (
        "Data-class artifact(s) found inside the engine clone -- the engine must "
        "stay code-only; these route private or are refused and belong in the DATA root "
        "(.heading-os-data) via the get_*_dir() seam:\n  " + "\n  ".join(flagged)
    )


# --- Positive branch: the detector actually fires ----------------------------


def test_detector_flags_a_private_data_path():
    # A real data path (outputs/) routes private -> must be flagged.
    assert find_data_artifacts(["outputs/operations/x.md"]) == ["outputs/operations/x.md"]


def test_detector_flags_private_outside_data_dirs():
    # Regression for the real leak (2026-06-22): docs/superpowers/ has top-level
    # 'docs' (not a data-dir name) yet routes `private`. The detector must flag any
    # private-routed path, not only those under a fixed data-dir allowlist.
    assert find_data_artifacts(["docs/superpowers/specs/x.md"]) == ["docs/superpowers/specs/x.md"]


def test_filter_skips_engine_routed_path():
    # The filter flags everything but engine. A data-dir-named path that routes
    # ENGINE (a hypothetical carve-out) must NOT be flagged. Proven with an injected
    # routing fn so the test does not depend on such a carve-out existing.
    assert find_data_artifacts(["datastore/brand/x.dotx"], routing_fn=lambda r: "engine") == []


def test_filter_flags_every_destination_but_engine():
    # The filter refuses anything that is not `engine`, not a list of refused
    # names. MEASURED 2026-09-25 against the version before HOS-132, which
    # refused `{"private", "corporate"}` by membership: the retired `corporate`
    # was flagged only because it was listed, and an unlisted answer passed
    # into the public repository. After, both are flagged.
    paths = ["datastore/intelligence/x.md"]
    for answer in ("corporate", "zzqx-unlisted"):
        assert find_data_artifacts(paths, routing_fn=lambda r, a=answer: a) == paths, answer


def test_detector_ignores_non_data_dir():
    # Engine code paths are never candidates.
    assert find_data_artifacts(["scripts/foo.py", "tests/bar.py", ".claude/rules/x.md"]) == []


# --- The demo tree is a closed manifest --------------------------------------
#
# Operator law, 2026-08-26: no data from the DATA repository may ever sit in the
# engine, and everything under `examples/` must be invented. The routing map has
# no entry for `examples/`, so before this section every one of the paths below
# fell through to the `engine` default and PASSED the wall. The route in is real:
# with no overlay `get_data_root()` answers `<workspace_root>/examples`, so a tool
# that writes to the data root writes inside the engine clone.
#
# Since HOS-131 (2026-09-25) the manifest is EMPTY: the engine ships no demo
# tree, so every path under `examples/` is flagged. The three tests about
# shipped demo files left with those files (HOS-131).


LEAKS_UNDER_THE_DEMO_TREE = [
    "examples/crm/contacts/real-person.md",
    "examples/outputs/operations/a-brief.md",
    "examples/knowledge/a-private-note.md",
    "examples/datastore/brand/logo.png",
    "examples/state/captured-mail-bodies.json",
    "examples/.sync/logs/audit.jsonl",
]


@pytest.mark.parametrize("rel", LEAKS_UNDER_THE_DEMO_TREE)
def test_a_file_that_is_not_shipped_demo_data_is_flagged_under_examples(rel):
    """Every one of these passed the wall until 2026-08-26."""
    assert find_data_artifacts([rel]) == [rel]


@pytest.mark.parametrize("spelling", [
    "/examples/crm/contacts/real-person.md",        # leading slash
    "//examples/crm/contacts/real-person.md",       # doubled, as a join can produce
    "examples\\crm\\contacts\\real-person.md",      # Windows separators
    "\\examples\\crm\\contacts\\real-person.md",    # both at once
])
def test_the_demo_branch_normalises_before_it_compares(spelling):
    """The demo-tree rule matches STRINGS, so the spelling has to be settled first.

    Every other rule in `find_data_artifacts` is decided by
    `get_routing_destination`, which normalises the separator and the leading
    slash itself, so those survive a caller that spells a path oddly. The
    `examples/` rule does not: it is a literal `startswith(DEMO_ROOT)` and a
    literal membership test against `DEMO_MANIFEST`, and it sees whatever it is
    handed.

    MEASURED 2026-09-01 by removing `rel.replace("\\\\", "/").lstrip("/")` from
    the function and re-running the 116 tests across this file, test_engine_guard
    and the five leak-wall neighbours: all 116 stayed GREEN, while

        find_data_artifacts(["/examples/crm/contacts/real.md"])   -> []
        find_data_artifacts(["examples\\\\crm\\\\contacts\\\\real.md"]) -> []

    against `['examples/crm/contacts/real.md']` for the same file spelled plainly.
    A contacts file under the bundled demo tree walked through the wall, which is
    the exact operator law of 2026-08-26 that the manifest section exists to
    enforce, and nothing in the suite noticed.

    The route in is not hypothetical. `scan_engine_repo` renders `extra_paths`
    with `str(p)`, and the push wall hands it the paths of the unpushed history;
    `str()` on a Path under Windows yields backslashes.
    """
    assert find_data_artifacts([spelling]) == ["examples/crm/contacts/real-person.md"], (
        f"a data file under the demo tree spelled {spelling!r} was cleared by the "
        "wall. The demo rule compares strings and cannot normalise them itself."
    )


def test_the_manifest_matches_the_tree_git_actually_carries():
    """A manifest that drifts from disk is a claim that has stopped being true.

    Asserted in BOTH directions on purpose. A file added to `examples/` and not to
    the manifest is caught by the tree-clean test above, loudly. A file REMOVED
    from `examples/` and left on the manifest is caught only here, and it matters:
    a manifest naming files that no longer exist reads as wider coverage than it
    has. With the manifest empty since HOS-131, this asserts that git carries
    nothing under `examples/` at all.
    """
    root = get_workspace_root()
    on_disk = {
        p for p in repo_carried_paths(root)
        if p.startswith("examples/")
    }
    assert on_disk == set(DEMO_MANIFEST), (
        "the shipped demo tree and DEMO_MANIFEST disagree.\n"
        f"  on disk, not on the manifest: {sorted(on_disk - set(DEMO_MANIFEST))}\n"
        f"  on the manifest, not on disk: {sorted(set(DEMO_MANIFEST) - on_disk)}"
    )


# --- The one engine file that carries data ADDRESSES to every clone ----------

#: A path segment holding an ISO date. A dated directory belongs to one run on
#: one tree; it is never structure, and the engine may not promise it.
_DATED_SEGMENT = re.compile(r"\d{4}-\d{2}-\d{2}")

#: The suffixes the shipped default may name a FILE with. Structure addresses
#: configuration and context records; an office document, an image or a database
#: is an operator's own artifact wearing an address.
_STRUCTURAL_SUFFIXES = frozenset({".md", ".json", ".jsonl", ".txt", ".yaml"})


def _operator_shaped(address: str) -> str | None:
    """Why `address` is one tree's own rather than every tree's, or None."""
    for segment in address.split("/"):
        if _DATED_SEGMENT.search(segment):
            return (f"segment {segment!r} carries a date: a dated directory is "
                    f"one run on one tree, not structure")
        stem, dot, suffix = segment.rpartition(".")
        if dot and stem and f".{suffix}" not in _STRUCTURAL_SUFFIXES:
            return (f"segment {segment!r} names a .{suffix} file; the default "
                    f"set addresses structure and the configuration and context "
                    f"records inside it, never one operator's document")
    return None


def test_the_shipped_default_registry_names_no_operator_address():
    """`scripts/data-names.example.yaml` goes to every clone of a PUBLIC repo.

    It is the one engine file whose whole content is data ADDRESSES, so it is
    the shortest path by which a private one leaves. MEASURED 2026-09-22, both
    gates that would be expected to hold that boundary pass over two entries
    that breach it -- one named team workbook under `datastore/operations/` and a dated
    pilot directory under `outputs/operations/`, taken from the live registry
    and deliberately left out of the shipped set -- because `content-guard.py`
    matches people and entities and `leak-guard.py` matches routing, and a dated
    directory or one workbook is neither. The refusing half below spells the
    workbook and the dated directory with invented names of the same shape. Both returned `clean`, rc=0. Nothing
    else refuses them, which is why this is a test and not a reader's note.

    It cannot establish that a generically-spelled address is not somebody's:
    that stays the author's judgement, and `content-guard` still holds the names
    and entities. What it holds is the two shapes an operator's address actually
    arrives in.
    """
    from scripts.utils.data_path import template_names

    entries = template_names()
    # Outside the loop: an emptied or unreadable file must fail here rather than
    # pass by asserting nothing. 69 on 2026-09-22.
    assert len(entries) >= 60, (
        f"the shipped default names only {len(entries)} address(es); it held 69 "
        f"on 2026-09-22, so this guard is measuring a file that has been gutted")
    offences = []
    for name, entry in sorted(entries.items()):
        address = "/".join(p for p in (entry.get("path") or "",
                                       entry.get("under") or "") if p)
        why = _operator_shaped(address)
        if why is not None:
            offences.append(f"{name}: {address} -- {why}")
    assert not offences, (
        "the engine's default data-address registry carries an address that "
        "belongs to one operator's tree, and this file ships to a public "
        "repository:\n  " + "\n  ".join(offences))

    # The other direction, on the same rule: it must refuse, or the pass above
    # is a guard that refuses nothing.
    assert _operator_shaped("datastore/operations/team/Acme_Team.xlsx")
    assert _operator_shaped("outputs/operations/example-pilot-2026-09-09")
    assert _operator_shaped("crm/contacts") is None
    assert _operator_shaped("context/pipeline.md") is None


def test_the_engine_holds_only_the_engine(tmp_path):
    """HOS-131, operator decision 2026-09-25: a tracked engine file that is not the
    engine is a leak, invented demo content included. Before this gate every path
    below passed every wall here, because each wall matches a known kind of
    private content and the demo tree was invented. MEASURED 2026-09-25 on this
    branch with the gate staged: 1315 tracked files, 1103 covered by the
    allowlist, 212 outside it and frozen in the shrink-only baseline. Both jaws,
    on the checker's own `judge` and on its CLI over the real tree."""
    import importlib.util
    import subprocess

    root = get_workspace_root()
    spec = importlib.util.spec_from_file_location("engine_only", root / "scripts" / "check-engine-only.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    classes = [{"class": "code", "reason": "code", "patterns": ["scripts/**/*.py"]}]

    def kinds(paths, baseline=(), refs=None, cls=classes, entries=()):
        for rel in paths:  # the checker reads a covered script's bytes since HOS-159
            (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / rel).touch()
        v = mod.judge(tmp_path, list(paths), cls, list(entries), set(baseline), refs or {})
        return sorted((k, p) for k, p, _ in v["findings"])

    # Refused: planted content, a stale baseline entry, a bought one, a content class allowed.
    assert kinds(["scripts/a.py", "examples/foo.md", "docs/whatever.md"]) == [
        ("not-engine", "docs/whatever.md"), ("not-engine", "examples/foo.md")]
    assert kinds(["scripts/a.py"], baseline={"examples/gone.md"}) == [("stale-baseline", "examples/gone.md")]
    assert kinds(["scripts/a.py", "examples/foo.md"], baseline={"examples/foo.md"},
                 refs={"HEAD": set()}) == [("baseline-rise", "examples/foo.md")]
    assert ("never", "examples/foo.md") in kinds(
        ["scripts/a.py", "examples/foo.md"], baseline={"examples/foo.md"},
        entries=[{"path": "examples/foo.md", "class": "x", "reason": "x"}])
    # Passed: an engine script, and a frozen file that is still outside.
    assert kinds(["scripts/a.py", "scripts/utils/b.py"]) == []
    assert kinds(["scripts/a.py", "records/r.md"], baseline={"records/r.md"},
                 refs={"HEAD": {"records/r.md"}}) == []
    # The real tree, through the entry point the commit and the push run.
    proc = subprocess.run([sys.executable, str(root / "scripts" / "check-engine-only.py")],
                          cwd=str(root), capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_a_script_carrying_a_document_is_refused(tmp_path):
    """HOS-159: the allowlist class `engine-code` covers `scripts/**/*.py` whole,
    so a document written out as a Python generator passed as engine. Nine
    scripts left for the data repository when this went in, one of them a
    personal letter. MEASURED 2026-09-28 over every tracked .py: the DOCX/PPTX
    builders that name one document at a data address were seven of the nine
    and nothing else; the other two, a retry list and a converter whose cover
    was one tender's, are held by the departed list alone. The largest
    literal prose in a legitimate builder was 789 words against 3090 in the
    smallest leak that carried its text inline. A script that left for the data
    repository is refused by path if it comes back."""
    import importlib.util

    root = get_workspace_root()
    spec = importlib.util.spec_from_file_location("engine_only", root / "scripts" / "check-engine-only.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    classes = [{"class": "code", "reason": "code", "patterns": ["scripts/**/*.py"]}]
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    builder = "def _ensure():\n    from docx import Document\n"
    (scripts / "named.py").write_text(
        builder + "OUT = data_path('document-output') / 'sub' / 'A Letter To Someone.docx'\n")
    (scripts / "prose.py").write_text(
        builder + "BODY = [\n" + "    'the platform delivers clear insight to every operator',\n" * 300 + "]\n")
    (scripts / "bound.py").write_text(
        "from scripts.utils.docx_helpers import load_docx\n"
        "def main():\n    docs = data_path('document-output')\n    return docs / 'a-letter.md'\n")
    (scripts / "tool.py").write_text(
        builder + "import sys\nOUT = data_path('document-output') / sys.argv[1]\nNOTE = 'a short caption'\n")
    (scripts / "plain.py").write_text("OUT = data_path('x') / 'a report.docx'\n")
    (scripts / "gone.py").write_text("print('x')\n")

    def kinds(paths, departed=()):
        v = mod.judge(tmp_path, list(paths), classes, [], set(), {}, departed=list(departed))
        return sorted((k, p) for k, p, _ in v["findings"])

    # Refused: a builder naming its one document, a builder carrying the text, a departed path.
    assert kinds(["scripts/named.py", "scripts/bound.py", "scripts/prose.py", "scripts/tool.py",
                  "scripts/plain.py"]) == [
        ("document-content", "scripts/bound.py"), ("document-content", "scripts/named.py"),
        ("document-content", "scripts/prose.py")]
    assert kinds(["scripts/gone.py"], departed=[{"path": "scripts/gone.py", "reason": "moved"}]) == [
        ("departed", "scripts/gone.py")]
    # Passed: a builder whose document comes from its caller, and a non-builder.
    assert kinds(["scripts/tool.py", "scripts/plain.py"]) == []


def test_a_skill_stating_a_business_figure_is_refused(tmp_path):
    """HOS-164: the class `engine-code` covers the skill tree whole, so a
    company's prices, hiring record and patent count in a SKILL.md passed as
    engine. MEASURED 2026-09-28: 21 lines in 10 skill files stated one on the
    tree before they left for the data repository, and none after. The same
    patterns match format examples in JSON, which is why Markdown is read only
    under the skill, rule, agent and reference trees. Python is read too since
    HOS-165, with its format examples exempt one by one; see the next test."""
    import importlib.util

    root = get_workspace_root()
    spec = importlib.util.spec_from_file_location("engine_only", root / "scripts" / "check-engine-only.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    classes = [{"class": "code", "reason": "code", "patterns": ["**"]}]
    planted = {
        ".claude/skills/a/SKILL.md": "- Bundle pricing: Tier A (1.0x) / Tier B (1.25x)\n",
        ".claude/skills/b/SKILL.md": "Hiring standard: 900+ interviews for ~12 hires\n",
        ".claude/skills/c/references/r.md": "- Unit economics: deployment value ($3.1M+)\n",
        ".claude/rules/d.md": "- Research Lab: 3 patents filed\n",
        "reference/e.md": "Support: Years 2-5 at 18% annually\n",
    }
    passed = {
        ".claude/skills/f/SKILL.md": "- Pricing: bandwidth x [$ per Gb/s]; support at the rate in the record\n",
        ".claude/skills/g/SKILL.md": "Precise numbers: $347,850 not $350,000; a draft costs $0.003\n",
        "scripts/x.md": "raised $9M\n",
    }
    for rel, body in {**planted, **passed}.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(body)

    def kinds(paths):
        v = mod.judge(tmp_path, list(paths), classes, [], set(), {})
        return sorted((k, p) for k, p, _ in v["findings"])

    assert kinds(planted) == sorted(("business-figure", p) for p in planted)
    assert kinds(passed) == []


def test_a_script_stating_a_business_figure_is_refused(tmp_path):
    """HOS-165: the business-figure gate read Markdown only, so a dashboard
    generator's market and fundraising defaults and a bot daemon's headcount
    passed as engine. MEASURED 2026-09-28: the patterns matched 9 lines in
    those two scripts and 7 format examples elsewhere in the engine's Python;
    the examples are exempt by path AND matched text, so the same file stating
    any other figure is still refused. tests/ is not read: its fixtures are
    invented figures planted so a gate can be seen to refuse."""
    import importlib.util

    root = get_workspace_root()
    spec = importlib.util.spec_from_file_location("engine_only", root / "scripts" / "check-engine-only.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    classes = [{"class": "code", "reason": "code", "patterns": ["**"]}]
    planted = {
        "scripts/a.py": 'DEFAULTS = {"tam": "$12.5B", "raised": "$4M"}\n',
        "scripts/b.py": 'print("populate it for all 40 employees")\n',
        ".claude/hooks/c.py": "# growth: 17.5% CAGR\n",
        "scripts/pipeline-summary.py": '"""Parse \'$500K\'."""\nTARGET = "$7M"\n',
    }
    passed = {
        "scripts/pipeline-summary.py": '"""Parse a money string like \'$500K\'."""\n',
        "tests/test_x.py": 'FIXTURE = "$9M"\n',
        "scripts/plain.py": 'TOTAL = f"${n:,}"\n',
    }

    def kinds(files):
        for rel, body in files.items():
            (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / rel).write_text(body)
        v = mod.judge(tmp_path, list(files), classes, [], set(), {})
        return sorted((k, p) for k, p, _ in v["findings"])

    assert kinds(planted) == sorted(("business-figure", p) for p in planted)
    assert kinds(passed) == []


def test_the_dashboard_states_no_figure_when_the_data_repository_has_none(tmp_path, monkeypatch):
    """HOS-165: scripts/generate-dashboard.py carried the market, headcount and
    fundraising figures as defaults, so a public clone with no data repository
    drew the operator's numbers. MEASURED 2026-09-28: with neither the metrics
    file nor the readings record present, every rendered figure came back as a
    literal from the engine. Now each says it is unknown, the readings record
    supplies what the metrics file lacks, and the metrics file wins over both.
    Every value below is invented."""
    import importlib.util

    root = get_workspace_root()
    spec = importlib.util.spec_from_file_location("dashboard", root / "scripts" / "generate-dashboard.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    metrics, readings = tmp_path / "current-data.md", tmp_path / "readings.json"
    monkeypatch.setattr(mod, "metrics_file", lambda: metrics)
    monkeypatch.setattr(mod, "metric_readings_file", lambda: readings, raising=False)

    absent = mod.collect_metrics()
    assert set(absent) >= {"headcount", "hiring_target", "modules_live", "dpi_tam_2030",
                           "mea_2030", "cis_2030", "predecessor_vacuum_countries", "cagr"}
    assert set(absent.values()) == {"n/a"}

    readings.write_text('{"mea_2030": "$9.99B", "headcount": "7"}')
    metrics.write_text("| Headcount | 12 |\n")
    got = mod.collect_metrics()
    assert (got["mea_2030"], got["headcount"], got["cagr"]) == ("$9.99B", "12", "n/a")


def test_the_comms_export_refuses_an_output_directory_inside_the_engine(
        tmp_path, monkeypatch):
    """F-3.p4.18: the mail and message export cannot write into this checkout.

    `scripts/comms-history-export.py` writes the operator's private mail and
    Telegram history wherever `--out` points. MEASURED 2026-10-16 against the
    previous version: `--out scripts` passed its only check (that the
    directory exists) and the export ran. Driven through `main()` with the
    source replaced by a stand-in, so no mailbox is contacted: an `--out`
    inside the checkout exits 2 before the source is asked, and an `--out`
    outside it reaches the source.
    """
    import importlib.util
    root = get_workspace_root()
    spec = importlib.util.spec_from_file_location(
        "comms_export_under_test", root / "scripts" / "comms-history-export.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    called: list = []
    monkeypatch.setattr(mod, "export_exchange",
                        lambda out, since, ahead: called.append(out) or {})

    inside = root / "scripts"
    monkeypatch.setattr(sys, "argv", ["x", "exchange", "--out", str(inside),
                                      "--since", "2026-01-01"])
    assert mod.main() == 2
    assert called == [], "the export reached its source with --out inside the engine"

    monkeypatch.setattr(sys, "argv", ["x", "exchange", "--out", str(tmp_path),
                                      "--since", "2026-01-01"])
    assert mod.main() == 0
    assert called == [tmp_path]


def test_a_teach_course_is_refused_inside_the_engine(tmp_path, monkeypatch):
    """/teach writes a course only under `teach-courses`, never in the engine.

    The upstream skill writes its course into the current directory, which from
    this checkout is the public engine. `scripts/teach.py` is the skill's only
    door to disk. MEASURED 2026-10-28 on a scratch pair: a data root inside the
    engine checkout exits 2 and creates nothing; a data root beside it creates
    the course. Driven through `main()`, with `WORKSPACE_ROOT` naming a stand-in
    engine so nothing is created inside the real one.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "teach_under_test", get_workspace_root() / "scripts" / "teach.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    registry = ('version: 1\nnames:\n  teach-courses:\n    path: knowledge/courses\n'
                '    what: "courses"\n')

    engine = tmp_path / "engine"
    inside = engine / "data"
    (inside / "config").mkdir(parents=True)
    (inside / "config" / "data-paths.yaml").write_text(registry, encoding="utf-8")
    monkeypatch.setenv("WORKSPACE_ROOT", str(engine))
    monkeypatch.setenv("HEADING_OS_DATA", str(inside))
    assert mod.main(["init", "demo"]) == 2
    assert not (inside / "knowledge").exists(), "a course was created inside the engine"

    beside = tmp_path / "data"
    (beside / "config").mkdir(parents=True)
    (beside / "config" / "data-paths.yaml").write_text(registry, encoding="utf-8")
    monkeypatch.setenv("HEADING_OS_DATA", str(beside))
    assert mod.main(["init", "demo"]) == 0
    assert (beside / "knowledge" / "courses" / "demo" / "lessons").is_dir()


def test_a_teach_course_symlinked_into_the_engine_is_refused(tmp_path, monkeypatch):
    """A course directory that is a symlink into the engine is refused, not followed.

    MEASURED 2026-10-28 (HELM review of c2008455): with `teach-courses/demo` a
    symlink to a directory inside the engine checkout, `teach.py init demo`
    passed the root-only check, created lessons/, reference/, learning-records/
    and assets/ inside the engine, and exited 0. The resolved course directory
    is now checked before anything follows it; a plain course beside it is
    still created.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "teach_symlink_under_test", get_workspace_root() / "scripts" / "teach.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    engine, data = tmp_path / "engine", tmp_path / "data"
    target = engine / "inside"
    target.mkdir(parents=True)
    (data / "config").mkdir(parents=True)
    (data / "config" / "data-paths.yaml").write_text(
        'version: 1\nnames:\n  teach-courses:\n    path: knowledge/courses\n'
        '    what: "courses"\n', encoding="utf-8")
    (data / "knowledge" / "courses").mkdir(parents=True)
    (data / "knowledge" / "courses" / "demo").symlink_to(target, target_is_directory=True)
    monkeypatch.setenv("WORKSPACE_ROOT", str(engine))
    monkeypatch.setenv("HEADING_OS_DATA", str(data))

    assert mod.main(["init", "demo"]) == 2
    assert list(target.iterdir()) == [], "a course was created inside the engine"

    assert mod.main(["init", "plain"]) == 0
    assert (data / "knowledge" / "courses" / "plain" / "lessons").is_dir()

    lessons = data / "knowledge" / "courses" / "plain" / "lessons"
    lessons.rmdir()
    lessons.symlink_to(target, target_is_directory=True)
    assert mod.main(["next", "plain", "lessons"]) == 2, "next numbered inside the engine"
