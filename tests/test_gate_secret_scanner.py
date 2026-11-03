"""GUARANTEE: a credential cannot be committed.

FAILURE A, a private byte reaching this public repository, in the one shape that
is irreversible the moment it lands: a live credential, which is compromised by
publication and stays compromised after the commit is reverted.

HOW THIS DRIVES THE GATE. Through `subprocess`, at `scripts/secret-scanner.py`,
which is byte for byte the `entry` line `.pre-commit-config.yaml` registers as
`secret-scanner-workspace` under `.venv/bin/python`. There is no installed second copy to
read by mistake, unlike a hook fetched from a remote repository.

NOT ONE PATTERN IS WRITTEN DOWN HERE. The rules are read from the gate at run
time through `iter_patterns`, and a firing sample for each is GENERATED from
that rule's own compiled form by walking its parse tree: literals emit
themselves, a character class donates one member, a bounded repeat runs its
minimum, an alternation takes its first branch, a lookaround is skipped. Each
sample is then checked against the rule that produced it, so a rule the walker
cannot handle arrives as a hard failure naming that rule rather than as a
quietly missing case. Three things follow, and the third is the one that
matters. There is no second list to drift. A rule REMOVED from the gate loses
its case silently, so the count is floored below with the number measured and
the date. And nothing in this public repository restates what the gate looks
for: a test that enumerates a guard's patterns enumerates, by the same act,
what is NOT among them, which is an evasion map published beside the guard.

THE HONEST CASE IS NOT INVENTED. `.pre-commit-config.yaml` excludes
`skills-lock.json` from the entropy scanner because it holds sha256 digests BY
CONSTRUCTION, and says in the same comment that it is not unguarded, because
THIS scanner reads it at commit and at push and reports it clean on the pattern
rules. That is a claim about this
gate, so it is asserted here rather than believed.

NOT CLAIMED. Nothing about the push-time wall in `scripts/push-all.py`, the
embedded copy of these patterns in `.claude/hooks/_dispatch.py`, the redactor
beside them, `--scan-dir`, `--stdin`, `--stdin0`, the allowlist marker, an
unreadable file, or a credential already in history. The samples are
structurally minimal and prove each rule FIRES; they say nothing about how
narrowly it fires, which is where a false positive would come from.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.utils.secret_patterns import REQUIRED_SUBSTRING, iter_patterns

try:  # the standard library's own regex parser, private but stable since 3.11
    import re._parser as _regex_parser
except ImportError:  # pragma: no cover - older interpreters
    try:
        import sre_parse as _regex_parser
    except ImportError:
        _regex_parser = None

ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / "scripts" / "secret-scanner.py"
CONFIG = ROOT / ".pre-commit-config.yaml"

# MEASURED 2026-09-21 against this tree: the gate carries 17 credential rules,
# and the walker below generates a firing sample for all 17. The floor is that
# measured number, asserted OUTSIDE every loop, because a loop over a rule list
# that came back short passes while checking less. A number below it means the
# gate refuses fewer credential shapes than it did.
RULE_FLOOR = 17

# The one rule below that is addressed by name, and the name is the DESCRIPTION
# the gate prints, never its pattern. Selecting by it is what lets the boundary
# case read its sample out of the gate instead of restating a key header here.
PEM_RULE = "PEM private key"
MARKDOWN_LABEL_RULE = "Plaintext password in markdown"

# Characters a generated sample draws on where a rule allows a choice. Chosen to
# carry the workspace's grep sentinel, so every byte this file writes to disk is
# visibly synthetic.
FILLER = "ZzQqXx09-_."


def _opname(op) -> str:
    return op.name if hasattr(op, "name") else str(op)


def _member_of(items) -> str:
    """One character satisfying a parsed character class."""
    if items and _opname(items[0][0]) == "NEGATE":
        banned: set[str] = set()
        for op, arg in items[1:]:
            name = _opname(op)
            if name == "LITERAL":
                banned.add(chr(arg))
            elif name == "RANGE":
                banned.update(chr(c) for c in range(arg[0], arg[1] + 1))
            elif name == "CATEGORY" and "SPACE" in _opname(arg):
                banned.update(" \t\n\r\f\v")
        for ch in FILLER:
            if ch not in banned:
                return ch
        raise NotImplementedError("no filler character satisfies a negated class")
    for op, arg in items:
        name = _opname(op)
        if name == "LITERAL":
            return chr(arg)
        if name == "RANGE":
            for code in range(arg[0], arg[1] + 1):
                if chr(code) in FILLER:
                    return chr(code)
            return chr(arg[0])
        if name == "CATEGORY":
            category = _opname(arg)
            if "SPACE" in category:
                return " "
            if "DIGIT" in category:
                return "0"
            if "WORD" in category:
                return "Z"
    raise NotImplementedError("character class shape not handled")


def _emit(node, out: list[str]) -> None:
    for op, arg in node:
        name = _opname(op)
        if name == "LITERAL":
            out.append(chr(arg))
        elif name == "NOT_LITERAL":
            out.append("A" if arg != ord("A") else "B")
        elif name == "IN":
            out.append(_member_of(arg))
        elif name == "ANY":
            out.append("A")
        elif name in ("MAX_REPEAT", "MIN_REPEAT"):
            low, _high, sub = arg
            for _ in range(max(low, 1)):
                _emit(sub, out)
        elif name == "SUBPATTERN":
            _emit(arg[3], out)
        elif name == "BRANCH":
            _emit(arg[1][0], out)
        elif name == "ASSERT":
            _emit(arg[1], out)
        elif name in ("ASSERT_NOT", "AT", "GROUPREF"):
            continue
        else:
            raise NotImplementedError(name)


def _sample_for(pattern) -> str:
    out: list[str] = []
    _emit(_regex_parser.parse(pattern.pattern, pattern.flags), out)
    return "".join(out)


@pytest.fixture(scope="module")
def samples() -> list[tuple[str, str]]:
    """(description, firing sample) for every rule the gate carries right now."""
    if _regex_parser is None:
        pytest.fail("no regular-expression parser is importable (tried "
                    "re._parser and sre_parse), so no sample can be generated "
                    "and nothing below tests anything")
    # `iter_patterns` prefilters on text, and the gate itself records which
    # needle each prefiltered rule requires. Asking the gate for those needles
    # and handing them all back is what makes this the FULL rule set rather
    # than a subset chosen by accident.
    probe = "".join(REQUIRED_SUBSTRING.values())
    rules = list(iter_patterns(probe))
    generated: list[tuple[str, str]] = []
    ungenerated: list[str] = []
    for pattern, description in rules:
        try:
            sample = _sample_for(pattern)
        except (NotImplementedError, RecursionError) as exc:
            ungenerated.append(f"{description}: {exc}")
            continue
        if pattern.search(sample):
            generated.append((description, sample))
        else:
            ungenerated.append(f"{description}: generated sample does not match")
    if ungenerated:
        pytest.fail("no firing sample could be generated for these rules, so "
                    "they are UNTESTED rather than passing: "
                    + "; ".join(ungenerated))
    assert len(generated) >= RULE_FLOOR, (
        f"the gate carries {len(generated)} credential rules with a firing "
        f"sample, below the {RULE_FLOOR} measured 2026-09-21. The loop below "
        f"iterates exactly this list, so a shorter one is a quieter gate under "
        f"a green suite.")
    return generated


def _run(*paths: str) -> subprocess.CompletedProcess:
    """The REAL entry point a commit reaches, not an imported helper."""
    return subprocess.run([sys.executable, str(GATE), *paths],
                          cwd=str(ROOT), capture_output=True, text=True,
                          timeout=300)


def test_a_sample_of_every_credential_rule_the_gate_carries_is_refused(
        samples, tmp_path):
    """One subprocess per rule. The assertion is the EXIT CODE.

    A message is prose the scanner may reword; the exit code is what stops the
    commit. The fixtures live in `tmp_path` and never inside the checkout: this
    gate reads a named path wherever it is, and a credential-shaped byte has no
    business inside a repository whose whole guarantee is that it holds none.
    """
    survivors: list[str] = []
    target = tmp_path / "planted.txt"
    for description, sample in samples:
        target.write_text(f"VALUE = {sample}\n", encoding="utf-8")
        if _run(str(target)).returncode != 1:
            survivors.append(description)
    target.unlink(missing_ok=True)
    assert not survivors, (
        "the gate passed a file carrying a credential shaped exactly like one "
        "of its own rules: " + ", ".join(survivors))


def test_the_pem_rule_fires_on_a_header_and_not_from_inside_a_longer_token(
        samples, tmp_path):
    """The PEM rule's boundary, both directions, through the real gate.

    Until 2026-09-22 this rule was the only entry in the gate's vocabulary with
    no boundary lookaround at all, so its literal fired wherever it stood,
    including from the middle of an unbroken run of token characters - a shape
    no key can be written as, and one a machine-generated listing that quotes a
    sample produces by itself. MEASURED that day before the fix: the wrapped
    sample below was refused (exit 1), which is this function's red half.

    THE SAMPLE IS STILL NOT WRITTEN DOWN. It is the one the module fixture
    generated from this rule's own compiled form, so there is no second copy of
    the header text in this repository and no evasion map beside the guard. The
    rule is SELECTED by the description the gate prints in its own output; a
    label is not a pattern, and the selection fails loudly rather than silently
    covering nothing if that label ever changes.

    EACH SIDE IS DRIVEN SEPARATELY, and that is not tidiness. A sample wrapped
    on BOTH sides is refused by either lookaround alone, so a mutant that drops
    one of them survives it. Planting the neighbour on one side at a time is
    what makes this function go red for each half. MUTATION-VERIFIED
    2026-09-22, 6 of 6 planted in the rule itself: the whole boundary removed,
    either lookaround removed on its own, a dash added to either lookaround,
    and the leading class narrowed to letters.

    NOT CLAIMED. Nothing about the embedded copy of this rule in
    `.claude/hooks/_dispatch.py`, which is a separate wall edited by hand in the
    same change and driven by nothing here.
    """
    matching = [s for description, s in samples if description == PEM_RULE]
    assert len(matching) == 1, (
        f"the gate carries {len(matching)} rules described {PEM_RULE!r}; this "
        f"function drives exactly one and tests nothing at any other count")
    sample = matching[0]
    probe = tmp_path / "planted.txt"

    def verdict(text: str) -> int:
        probe.write_text(text + "\n", encoding="utf-8")
        return _run(str(probe)).returncode

    # The direction that must never weaken: the header on its own line, and the
    # same header against one more delimiter dash on either side. A dash is the
    # literal's OWN delimiter, so it is deliberately not in either lookaround,
    # and these two cases are what stop it being added there later.
    missed = [label for label, text in (
        ("on its own line", sample + "\nZZQXsyntheticbodyZZQX"),
        ("with a sixth leading dash", "-" + sample),
        ("with a sixth trailing dash", sample + "-"),
    ) if verdict(text) != 1]
    assert not missed, (
        "the gate passed a file carrying the key header shape this rule exists "
        "for: " + ", ".join(missed))

    # The direction that was wrong until 2026-09-22, one side at a time, over
    # the three members of the boundary class. The floor sits outside the
    # comprehension for the usual reason: a corpus that came back empty would
    # pass while checking nothing.
    neighbours = ("Z", "9", "_")
    assert len(neighbours) == 3
    survivors = [f"{side} {neighbour!r}"
                 for neighbour in neighbours
                 for side, text in (
                     ("before", f"VALUE = {neighbour * 4}{sample}"),
                     ("after", f"VALUE = {sample}{neighbour * 4}"))
                 if verdict(text) != 0]
    assert not survivors, (
        "the gate refused a run of token characters that merely SPELLS the key "
        "header; no key can carry a word character against its delimiter: "
        + ", ".join(survivors))


def test_the_markdown_password_rule_keeps_its_exclusions_after_a_run_of_whitespace(
        samples, tmp_path):
    """The markdown password rule's exclusions hold after any whitespace run.

    The whitespace run after the label is greedy but not atomic, so until
    2026-11-01 a run of two or more whitespace characters let the engine back
    off by one, test the exclusion lookahead against a space instead of the
    excluded word, and match that space plus the word as the value. Whitespace
    is now the first alternative of that lookahead, so it is always tested at
    the first non-whitespace character. MEASURED 2026-11-01 before the fix: the
    honest case below, two spaces, the excluded word, then the sample's own
    eight-character value, was refused (exit 1), which is this function's red
    half. The refused half is green before and after: it is what stops the fix
    from being widened into a loosening of the value check.

    THE SAMPLE AND THE WORD ARE NOT WRITTEN DOWN. The sample is the one the
    module fixture generated from this rule's compiled form; the variants widen
    its FIRST space, which the label does not contain. The excluded word is
    read out of the same compiled form: the first alternative of its exclusion
    lookahead that is a run of literals.

    MUTATION-VERIFIED 2026-11-01, 2 of 2 planted in the rule itself: the
    whitespace alternative dropped from the lookahead (which is also the rule
    as it stood before this change), and the whitespace run narrowed to a run
    of literal spaces.

    NOT CLAIMED. Nothing about the embedded copy of this rule in
    `.claude/hooks/_dispatch.py` (edited by hand in the same change and checked
    by a reproduction run, not by this function), or the redactor. An excluded
    word followed by more than one space or by a tab still fires; that shape is
    left open. And the short-value residual of this change: a value of fewer
    than eight characters after two or more whitespace characters, refused
    before, is no longer refused, because the rule's eight-character minimum
    now applies after the whole run, as it always did after one space.
    """
    matching = [s for description, s in samples
                if description == MARKDOWN_LABEL_RULE]
    assert len(matching) == 1, (
        f"the gate carries {len(matching)} rules described "
        f"{MARKDOWN_LABEL_RULE!r}; this function drives exactly one and "
        f"tests nothing at any other count")
    sample = matching[0]
    assert " " in sample, "the generated sample carries no space to widen"

    rules = [p for p, d in iter_patterns(sample) if d == MARKDOWN_LABEL_RULE]
    assert len(rules) == 1
    words: list[str] = []
    for op, arg in _regex_parser.parse(rules[0].pattern, rules[0].flags):
        if _opname(op) != "ASSERT_NOT":
            continue
        for branch_op, branch_arg in arg[1]:
            if _opname(branch_op) != "BRANCH":
                continue
            for alternative in branch_arg[1]:
                if alternative and all(_opname(o) == "LITERAL"
                                       for o, _a in alternative):
                    words.append("".join(chr(a) for _o, a in alternative))
                    break
    assert words, "no literal exclusion could be read out of the rule"
    word = words[0]

    probe = tmp_path / "planted.txt"

    def verdict(text: str) -> int:
        probe.write_text(text + "\n", encoding="utf-8")
        return _run(str(probe)).returncode

    # The direction that must never weaken: the sample's own value after a
    # wider run of whitespace is still a password.
    widened = [("two spaces", "  "), ("a tab", "\t"), ("a tab and a space", "\t ")]
    assert len(widened) == 3
    missed = [label for label, run in widened
              if verdict(sample.replace(" ", run, 1)) != 1]
    assert not missed, (
        "the gate passed the rule's own value after a wider whitespace run: "
        + ", ".join(missed))

    # The direction that was wrong until 2026-11-01. The value after the word
    # is the sample's own eight characters, because the word alone after one
    # backtracked space falls below the rule's minimum and would not have been
    # refused before the fix either.
    assert verdict(sample.replace(" ", "  " + word, 1)) == 0, (
        "the gate refused an excluded word after two spaces; the whitespace "
        "run backtracked around the exclusion lookahead")


def test_an_ordinary_file_with_no_credential_passes(tmp_path):
    """The same direction on synthetic bytes, so the honest case does not rest
    entirely on one file somebody may one day rewrite."""
    target = tmp_path / "honest.txt"
    target.write_text("VALUE = zzqx ordinary configuration, nothing secret\n",
                      encoding="utf-8")
    result = _run(str(target))
    assert result.returncode == 0, (
        f"the gate refused a file with no credential in it:\n{result.stdout}\n"
        f"{result.stderr}")


def test_the_gate_is_registered_in_the_config_that_arms_it(samples):
    """A gate nothing invokes refuses nothing, whatever its code says.

    Registration only. Whether the registration's `files:` pattern selects
    anything is `scripts/check-gate-integrity.py`'s, and it runs on every commit.
    """
    config = CONFIG.read_text(encoding="utf-8")
    assert "secret-scanner.py" in config, (
        f"secret-scanner.py is not registered in {CONFIG.name}, so nothing runs "
        f"it at commit time and the cases above prove only that the gate exists "
        f"on disk")
