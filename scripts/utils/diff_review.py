"""diff_review - one reviewer pass over a git diff, shared by two CLIs.

`scripts/review-eval.py` scores reviewer configurations against the defect set
harvested from Fix-Note commits; `scripts/review-branch.py` reviews a yard
branch before HELM merges it. Both send the same prompt through the same seam,
so a configuration that scores well in the eval is the one the branch review
runs, not a sibling of it.

The review rules travel IN the prompt. `REVIEW.md` is read only by the managed
Code Review product, never by a local Claude Code session (found 2026-10-12),
so a rule file on disk would be a rule nobody applies.

A Claude model is called directly, through Claude Code itself (`claude -p`,
the operator's subscription); every other model through
`scripts/utils/proxy_transport.call_model` and CLIProxyAPI (M045.1). Operator
instruction 2026-10-31: Anthropic models are always called directly, and every
other model always goes through the CLI proxy. Which models a
configuration may name is the named trust list (M009.1, `is_trusted`), and
anything else is refused before a byte leaves the machine.

The diff the model sees carries the NEW-side line number of every line, so a
finding's `line` is read off the page rather than computed from hunk headers,
which is where a reviewer's line numbers usually go wrong.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

# ============================================================
# Configuration
# ============================================================

# The named remote trust list (M009.1) as proxy model ids: Anthropic Claude,
# OpenAI ChatGPT Astra, Moonshot Kimi K3. A prefix admits a Claude release.
TRUSTED_PREFIXES = ("claude-",)
TRUSTED_EXACT = ("gpt-6-astra", "k3")

SEVERITIES = ("bug", "security", "nit")
BLOCKING = ("bug", "security")
NIT_CAP = 3

# Files a code review has nothing to say about; skipped in every diff.
SKIP_SUFFIXES = (".lock", ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".ico", ".woff", ".woff2")

RULES = """Review rules. Follow every one.
1. Report only a defect that makes the code do the wrong thing: wrong result, crash, data loss, a security hole, a guard that does not refuse, a resource or process left behind. Style, naming, wording, formatting, missing tests and missing documentation are not defects.
2. Every finding names a file and a line taken from the numbered diff below (the number at the left of a line is its line in the new file). A finding you cannot place on a line is not reported.
3. For each finding say what failure looks like: the concrete input or state, and the wrong outcome it produces.
4. Name the class of the defect in two to five words (for example "unchecked None", "off-by-one", "path escapes root", "exception swallowed"), so the author can look for siblings of it.
5. Severity is one of: bug, security, nit. A nit is anything that would not change behaviour; report at most 3 nits.
6. Judge the change against its stated intent below. Code that does what the intent says is not a defect because you would have done it differently.
7. If nothing needs fixing, say so plainly: count 0. Do not invent findings to have something to say.
8. Do not repeat a finding listed under "Already reported".
9. Mark a finding "pre_existing": true when the defect was already there before this change and the change neither introduced it nor made it reachable; it is still reported, so the author knows, but it is not the author's to fix in this change."""

PLAIN = """Review this change for bugs."""

OUTPUT = """Answer with ONE JSON object and nothing else:
{"count": <number of bug and security findings>,
 "summary": "<one sentence; starts with the count; says plainly when nothing needs fixing>",
 "findings": [{"file": "<path as in the diff>", "line": <new-file line>, "end_line": <new-file line>,
               "severity": "bug|security|nit", "class": "<defect class>",
               "pre_existing": false, "title": "<one line>", "failure": "<input or state -> wrong outcome>"}]}"""


@dataclass(frozen=True)
class Config:
    """One reviewer configuration: a model and a prompt shape, or a panel.

    A panel (`hunters` set) runs each hunter configuration on the same diff in
    parallel, merges what they found, and keeps a bug or security finding only
    when at least `quorum` of the `verifiers` judge it real (the vote of
    Anthropic's Code Review, claude.dev blog 2026-10-15). Its `model` is only a
    label; `models` lists what it actually calls.
    """

    name: str
    model: str
    rules: bool = True
    max_tokens: int = 16384
    timeout: float = 600.0
    hunters: tuple[str, ...] = ()
    verifiers: tuple[str, ...] = ()
    quorum: int = 0

    @property
    def models(self) -> list[str]:
        if not self.hunters:
            return [self.model]
        return [CONFIGS[n].model for n in (*self.hunters, *self.verifiers)]


# The configurations the eval compares. Add one here; the eval and the branch
# review both read this table.
CONFIGS = {
    c.name: c
    for c in (
        Config("sonnet-rules", "claude-sonnet-5-5"),
        Config("sonnet-plain", "claude-sonnet-5-5", rules=False),
        Config("opus-rules", "claude-opus-5-5"),
        Config("astra-rules", "gpt-6-astra"),
        Config("k3-rules", "k3", max_tokens=32768, timeout=900.0),
        # The three single configurations that located the most defects on
        # 2026-10-18 hunt; three different models vote, two of three keep.
        Config("panel-vote", "panel", hunters=("astra-rules", "opus-rules", "sonnet-plain"),
               verifiers=("astra-rules", "opus-rules", "k3-rules"), quorum=2),
        # The same vote on the two non-Claude models of the trust list: what still
        # runs when the proxy's Claude provider is down; both must agree.
        Config("panel-astra-k3", "panel", hunters=("astra-rules", "k3-rules"),
               verifiers=("astra-rules", "k3-rules"), quorum=2),
    )
}


def is_trusted(model: str) -> bool:
    return model in TRUSTED_EXACT or model.startswith(TRUSTED_PREFIXES)


# The parent session's own variables would point the child at this checkout.
CHILD_ENV_DROP = ("CLAUDE_PROJECT_DIR", "WORKSPACE_ROOT", "HEADING_OS_YARD")
# `claude -p` thinks at length by default; the proxy calls the configurations were
# scored with did not. MEASURED 2026-10-31, opus-rules on the d7d6b812 review
# prompt (75,024 diff chars): default effort timed out at 600 s, medium took
# 1,009 s and 129,046 output tokens for a 188-char answer, low took 19 s and
# 1,397 tokens with a well-formed answer.
CLAUDE_EFFORT = "low"


def call(model: str, prompt: str, *, max_tokens: int, timeout: float) -> str:
    """The answer text; a Claude model through `claude -p`, any other through the proxy.

    Raises RuntimeError on any failure, as `proxy_transport.call_model` does. The
    `claude -p` child runs in an empty directory with project settings only, no
    tools, no MCP servers and no hooks, so it answers one prompt and reads nothing. It has
    no temperature setting; `max_tokens` is the proxy's alone.
    """
    if not model.startswith(TRUSTED_PREFIXES):
        from scripts.utils.proxy_transport import call_model

        return call_model(model, prompt, temperature=0.0, max_tokens=max_tokens, timeout=timeout)
    from scripts.utils.data_path import data_write_dir

    env = {k: v for k, v in os.environ.items() if k not in CHILD_ENV_DROP}
    argv = ["claude", "-p", "--model", model, "--output-format", "json", "--max-turns", "1",
            "--tools", "", "--strict-mcp-config", "--setting-sources", "project", "--effort", CLAUDE_EFFORT,
            "--settings", json.dumps({"disableAllHooks": True})]
    with tempfile.TemporaryDirectory(prefix="diff-review-", dir=data_write_dir("runtime:scratch/tmp")) as d:
        try:
            r = subprocess.run(argv, input=prompt, cwd=d, capture_output=True, text=True,
                               timeout=timeout, env=env)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise RuntimeError(f"claude -p {model}: {type(e).__name__}: {e}") from e
    try:
        obj = json.loads(r.stdout)
    except json.JSONDecodeError:
        obj = {}
    if r.returncode != 0 or obj.get("is_error") or not obj.get("result"):
        raise RuntimeError(f"claude -p {model} returned no answer (exit {r.returncode}): "
                           f"{(r.stdout + r.stderr)[-300:]}")
    return obj["result"]


# ============================================================
# Diff rendering
# ============================================================

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def git(repo: Path, *args: str) -> str:
    """Run git in `repo` and return stdout; a failure raises with git's stderr."""
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def raw_diff(repo: Path, base: str, head: str, paths: list[str] | None = None, context: int = 12,
             *, prose_context: int | None = None) -> str:
    """`git diff base head`, rename-aware, restricted to `paths` when given.

    With `prose_context` set to a value other than `context`, and no `paths`,
    git runs twice over the same range: every file except `*.md` with
    `-U{context}`, then the `*.md` files with `-U{prose_context}`, the second
    output after the first. A file renamed between a `.md` and a non-`.md` name
    then shows as a delete in one pass and an add in the other.
    """
    args = ["diff", f"-U{context}", "-M", "--no-color", base, head]
    if paths is not None:
        if not paths:
            return ""
        args += ["--", *paths]
    if paths is not None or prose_context is None or prose_context == context:
        return git(repo, *args)
    code = git(repo, *args, "--", ":(exclude)*.md")
    prose = git(repo, "diff", f"-U{prose_context}", "-M", "--no-color", base, head, "--", "*.md")
    return code + prose


def numbered(diff: str, max_chars: int = 120_000) -> tuple[str, list[str], bool]:
    """Render a unified diff with the new-file line number at the left of each line.

    Returns (text, files, truncated). A file whose suffix is in SKIP_SUFFIXES
    is dropped; binary files carry no hunks and drop out by themselves. A
    deleted file is kept under its old path, marked "(deleted)": removing a
    module its callers still import is a change to review.

    The `---`/`+++` file headers are read only between `diff --git` and the
    first hunk; inside a hunk a removed line reading "-- x" arrives as "--- x"
    and is content, not a header.
    """
    blocks = _blocks(diff)
    files = [path for path, _ in blocks]
    text = "\n".join(ln for _, block in blocks for ln in block)
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars] + "\n" + TRUNCATED_MARK
    return text, files, truncated


TRUNCATED_MARK = "[... diff truncated here; nothing below this line was reviewed ...]"


def _blocks(diff: str) -> list[tuple[str, list[str]]]:
    """Each kept file of a unified diff as (path, its `=== <label>` line and rendered lines), in diff order."""
    blocks: list[tuple[str, list[str]]] = []
    out: list[str] = []
    new_no = 0
    path = old = None
    skipping = header = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            path = old = None
            skipping, header = False, True
            continue
        if header and line.startswith("--- "):
            source = line[4:]
            old = source[2:] if source.startswith("a/") else source
            continue
        if header and line.startswith("+++ "):
            target = line[4:]
            path = target[2:] if target.startswith("b/") else target
            label = path
            if path == "/dev/null":
                path, label = old, f"{old} (deleted)"
            skipping = path in (None, "/dev/null") or path.lower().endswith(SKIP_SUFFIXES)
            if not skipping:
                out = [f"=== {label}"]
                blocks.append((path, out))
            continue
        if header and line.startswith(("index ", "similarity ", "dissimilarity ", "rename ", "copy ", "new file",
                                       "deleted file", "old mode", "new mode", "Binary files")):
            continue
        m = _HUNK.match(line)
        if m:
            header = False
        if skipping or path is None:
            continue
        if m:
            new_no = int(m.group(1))
            out.append(line)
            continue
        if line.startswith("+"):
            out.append(f"{new_no:>6} + {line[1:]}")
            new_no += 1
        elif line.startswith("-"):
            out.append(f"{'':>6} - {line[1:]}")
        elif line.startswith("\\"):
            continue
        else:
            out.append(f"{new_no:>6}   {line[1:]}")
            new_no += 1
    return blocks


def order_key(path: str) -> tuple[int, str]:
    """Review order: code under `scripts/` and `.claude/hooks/` first, prose (`*.md`) last, the rest between."""
    if path.endswith(".md"):
        return (2, path)
    if path.startswith(("scripts/", ".claude/hooks/")):
        return (0, path)
    return (1, path)


def numbered_parts(diff: str, max_chars: int = 120_000) -> tuple[list[str], list[str], bool, list[list[str]]]:
    """Render a unified diff as `numbered()` does, split at file boundaries into parts.

    Returns (parts, files, truncated, part_files). Files are sorted by `order_key` and
    packed greedily, in that order, into parts joined with a newline, each part
    at most `max_chars` long. A single file longer than `max_chars` is a part
    of its own, cut to fit with the truncation marker; `truncated` is True only
    when that happened. `files` is in the sorted order; `part_files[k]` lists
    the paths rendered in `parts[k]`, in order.
    """
    blocks = sorted(_blocks(diff), key=lambda b: order_key(b[0]))
    files = [path for path, _ in blocks]
    parts: list[str] = []
    part_files: list[list[str]] = []
    current = ""
    current_files: list[str] = []
    truncated = False
    for path, block in blocks:
        text = "\n".join(block)
        if len(text) > max_chars:
            if current:
                parts.append(current)
                part_files.append(current_files)
                current, current_files = "", []
            keep = max_chars - len("\n" + TRUNCATED_MARK)
            parts.append(text[:keep] + "\n" + TRUNCATED_MARK)
            part_files.append([path])
            truncated = True
        elif not current:
            current, current_files = text, [path]
        elif len(current) + 1 + len(text) <= max_chars:
            current += "\n" + text
            current_files.append(path)
        else:
            parts.append(current)
            part_files.append(current_files)
            current, current_files = text, [path]
    if current:
        parts.append(current)
        part_files.append(current_files)
    return parts, files, truncated, part_files


# ============================================================
# The review call
# ============================================================


@dataclass
class Review:
    findings: list[dict]
    summary: str
    seconds: float
    prompt_chars: int
    answer_chars: int
    error: str | None = None
    dropped_nits: int = 0
    raw: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def blocking(self) -> list[dict]:
        return [f for f in self.findings if blocks(f)]


def blocks(f: dict) -> bool:
    """A bug or security finding the change itself is answerable for; a pre-existing one never blocks."""
    return f.get("severity") in BLOCKING and not f.get("pre_existing")


def build_prompt(cfg: Config, diff_text: str, intent: str, already: list[dict] | None = None) -> str:
    parts = [RULES if cfg.rules else PLAIN, "", OUTPUT, "", "Stated intent of the change:", intent.strip() or "(none given)"]
    if already:
        parts += ["", "Already reported (do not repeat). Add to your JSON object a key \"resolved\": the list "
                  "of the ids below that this diff visibly fixes, and leave out any it does not:"]
        parts += [f"- R{i} {f.get('file')}:{f.get('line')} {f.get('title')}" for i, f in enumerate(already, 1)]
    parts += ["", "The diff, new-file line numbers at the left:", diff_text]
    return "\n".join(parts)


def parse_answer(text: str) -> dict:
    """The JSON object in a model answer, fences and prose around it tolerated.

    The `findings` key is required: an object without it (an error message, an
    unrelated JSON) is off contract, never a review that found nothing.
    """
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in the answer")
    obj = json.loads(text[start:end + 1])
    if not isinstance(obj, dict) or not isinstance(obj.get("findings"), list):
        raise ValueError("answer is not the findings object")
    return obj


def _clean(f: dict) -> dict | None:
    """One finding normalised, or None when it names no file and line (rule 2)."""
    try:
        line = int(f.get("line"))
    except (TypeError, ValueError):
        return None
    file = str(f.get("file") or "").strip()
    if not file:
        return None
    try:
        end = int(f.get("end_line") or line)
    except (TypeError, ValueError):
        end = line
    sev = str(f.get("severity") or "bug").lower()
    return {
        "file": file.removeprefix("b/"),
        "line": line,
        "end_line": max(end, line),
        "severity": sev if sev in SEVERITIES else "bug",
        "class": str(f.get("class") or "").strip(),
        "pre_existing": f.get("pre_existing") is True,
        "title": str(f.get("title") or "").strip(),
        "failure": str(f.get("failure") or "").strip(),
    }


def review(cfg: Config, diff_text: str, intent: str, already: list[dict] | None = None) -> Review:
    """One reviewer pass, or a panel's. A transport or parse failure is returned, never raised."""
    for m in cfg.models:
        if not is_trusted(m):
            raise ValueError(f"model {m!r} is not on the named trust list (M009.1)")
    if cfg.hunters:
        return panel_review(cfg, diff_text, intent, already)
    prompt = build_prompt(cfg, diff_text, intent, already)
    t0 = time.monotonic()
    try:
        answer = call(cfg.model, prompt, max_tokens=cfg.max_tokens, timeout=cfg.timeout)
    except RuntimeError as e:
        return Review([], "", time.monotonic() - t0, len(prompt), 0, error=f"transport: {e}")
    seconds = time.monotonic() - t0
    try:
        obj = parse_answer(answer)
    except (ValueError, json.JSONDecodeError) as e:
        return Review([], "", seconds, len(prompt), len(answer), error=f"parse: {e}", raw=answer)
    found, bad = [], 0
    for f in obj["findings"]:
        c = _clean(f) if isinstance(f, dict) else None
        if c:
            found.append(c)
        elif not (isinstance(f, dict) and str(f.get("severity") or "").lower() == "nit"):
            bad += 1
    if bad:
        # A blocking finding that names no file and line cannot be dropped as
        # if it were never raised: the answer is off contract (exit 3).
        return Review([], "", seconds, len(prompt), len(answer), raw=answer,
                      error=f"parse: {bad} finding(s) without a file and line, or not an object")
    nits = [f for f in found if f["severity"] == "nit"]
    kept = [f for f in found if f["severity"] != "nit"] + nits[:NIT_CAP]
    resolved = obj.get("resolved")
    ids = sorted({str(x) for x in resolved}) if isinstance(resolved, list) else []
    return Review(kept, str(obj.get("summary") or ""), seconds, len(prompt), len(answer),
                  dropped_nits=max(0, len(nits) - NIT_CAP), raw=answer, extra={"resolved": ids})


# ============================================================
# The panel: several hunters, then a vote
# ============================================================

VERIFY = """You are verifying findings that other reviewers raised on a change. For each candidate, read the diff and decide:
- "real": true only if the defect exists as described and makes the code do the wrong thing (wrong result, crash, data loss, a security hole, a guard that does not refuse, a resource or process left behind). Style, naming, missing tests, missing documentation and a different taste are not real.
- "pre_existing": true if the defect was there before this change and the change neither introduced it nor made it reachable.
Judge against the stated intent: code that does what the intent says is not a defect.
Answer with ONE JSON object and nothing else, one verdict per candidate:
{"verdicts": [{"id": "C1", "real": true, "pre_existing": false, "why": "<one line>"}]}"""

NEAR = 3  # lines: two hunters' findings this close in one file are one candidate


def _same(a: dict, b: dict) -> bool:
    return a["file"] == b["file"] and a["line"] - NEAR <= b["end_line"] and b["line"] - NEAR <= a["end_line"]


def _verify(cfg: Config, diff_text: str, intent: str, cands: list[dict]) -> tuple[dict, int, int, str | None]:
    """One verifier's verdicts by candidate id, with prompt and answer sizes; an error string when it failed."""
    lines = [VERIFY, "", "Stated intent of the change:", intent.strip() or "(none given)", "", "Candidates:"]
    lines += [f"- C{i} {c['file']}:{c['line']}-{c['end_line']} [{c['severity']}] ({c['class']}) {c['title']}. "
              f"Failure: {c['failure']}" for i, c in enumerate(cands, 1)]
    prompt = "\n".join(lines + ["", "The diff, new-file line numbers at the left:", diff_text])
    try:
        answer = call(cfg.model, prompt, max_tokens=cfg.max_tokens, timeout=cfg.timeout)
    except RuntimeError as e:
        return {}, len(prompt), 0, f"verifier {cfg.name} transport: {e}"
    start, end = answer.find("{"), answer.rfind("}")
    try:
        obj = json.loads(answer[start:end + 1]) if 0 <= start < end else None
    except json.JSONDecodeError:
        obj = None
    if not isinstance(obj, dict) or not isinstance(obj.get("verdicts"), list):
        return {}, len(prompt), len(answer), f"verifier {cfg.name} parse: no verdicts object"
    out = {str(v.get("id")): v for v in obj["verdicts"] if isinstance(v, dict)}
    # A missing or non-boolean verdict is no vote at all: counted either way it
    # would pass for agreement, so the verifier is treated as having failed.
    unanswered = [f"C{i}" for i in range(1, len(cands) + 1)
                  if not isinstance(out.get(f"C{i}", {}).get("real"), bool)]
    if unanswered:
        return out, len(prompt), len(answer), f"verifier {cfg.name} gave no verdict on {', '.join(unanswered)}"
    return out, len(prompt), len(answer), None


def panel_review(cfg: Config, diff_text: str, intent: str, already: list[dict] | None = None) -> Review:
    """Hunters in parallel, candidates merged, each bug or security candidate put to the vote.

    Fails closed: any hunter or verifier that errs makes the whole panel an
    error, because a vote with a missing voter is not the vote configured. A
    verifier that leaves a candidate without a boolean verdict has failed too,
    so an omission neither drops a finding nor confirms one.
    """
    t0 = time.monotonic()
    hunters = [CONFIGS[n] for n in cfg.hunters]
    with ThreadPoolExecutor(max_workers=len(hunters)) as pool:
        runs = list(pool.map(lambda h: review(h, diff_text, intent, already), hunters))
    p_chars = sum(r.prompt_chars for r in runs)
    a_chars = sum(r.answer_chars for r in runs)
    raw = "\n".join(r.raw for r in runs)
    bad = [f"hunter {h.name}: {r.error}" for h, r in zip(hunters, runs, strict=True) if r.error]
    if bad:
        return Review([], "", time.monotonic() - t0, p_chars, a_chars, error="; ".join(bad), raw=raw)
    cands: list[dict] = []
    for h, r in zip(hunters, runs, strict=True):
        for f in r.findings:
            twin = next((c for c in cands if c["severity"] == f["severity"] and _same(c, f)), None)
            if twin:
                twin["found_by"].append(h.name)
            else:
                cands.append({**f, "found_by": [h.name]})
    nits = [c for c in cands if c["severity"] == "nit"]
    hard = [c for c in cands if c["severity"] != "nit"]
    kept: list[dict] = []
    if hard:
        voters = [CONFIGS[n] for n in cfg.verifiers]
        with ThreadPoolExecutor(max_workers=len(voters)) as pool:
            votes = list(pool.map(lambda v: _verify(v, diff_text, intent, hard), voters))
        p_chars += sum(v[1] for v in votes)
        a_chars += sum(v[2] for v in votes)
        bad = [v[3] for v in votes if v[3]]
        if bad:
            return Review([], "", time.monotonic() - t0, p_chars, a_chars, error="; ".join(bad), raw=raw)
        for i, c in enumerate(hard, 1):
            said = [v[0][f"C{i}"] for v in votes]
            real = [s for s in said if s["real"] is True]
            if len(real) >= cfg.quorum:
                pre = sum(1 for s in real if s.get("pre_existing") is True) * 2 > len(real)
                kept.append({**c, "pre_existing": pre, "votes": f"{len(real)}/{len(voters)}"})
    ids = [x for r in runs for x in r.extra.get("resolved", [])]
    resolved = sorted({x for x in ids if ids.count(x) * 2 > len(runs)})
    summary = (f"{len(hard)} candidate(s) from {len(hunters)} hunters, {len(kept)} kept by the vote "
               f"(quorum {cfg.quorum} of {len(cfg.verifiers)}).")
    return Review(kept + nits[:NIT_CAP], summary, time.monotonic() - t0, p_chars, a_chars,
                  dropped_nits=max(0, len(nits) - NIT_CAP), raw=raw,
                  extra={"resolved": resolved, "candidates": len(hard), "rejected": len(hard) - len(kept)})


def est_tokens(chars: int) -> int:
    """A token ESTIMATE at four characters a token; the proxy reports no usage to this seam."""
    return round(chars / 4)


# ============================================================
# Summary, count first
# ============================================================


def summary_lines(findings: list[dict], dropped_nits: int = 0) -> list[str]:
    """The count-first summary both CLIs print."""
    block = [f for f in findings if blocks(f)]
    old = [f for f in findings if f["severity"] in BLOCKING and f.get("pre_existing")]
    nits = [f for f in findings if f["severity"] == "nit"]
    if not block:
        head = f"0 findings need fixing. Nothing blocks the merge. ({len(nits)} nit(s))"
    else:
        bugs = sum(1 for f in block if f["severity"] == "bug")
        sec = len(block) - bugs
        head = f"{len(block)} finding(s) need fixing before merge ({bugs} bug, {sec} security); {len(nits)} nit(s)."
    if old:
        head += f" {len(old)} pre-existing defect(s) reported, not blocking."
    lines = [head]
    classes = sorted({f["class"] for f in block if f.get("class")})
    if classes:
        lines.append("Fix each class wherever it occurs in the change, not only at the line named: "
                     + "; ".join(classes))
    if dropped_nits:
        lines.append(f"({dropped_nits} further nit(s) dropped by the cap of {NIT_CAP})")
    order = {s: i for i, s in enumerate(("security", "bug", "nit"))}
    for f in sorted(findings, key=lambda f: (bool(f.get("pre_existing")), order[f["severity"]], f["file"], f["line"])):
        loc = f"{f['file']}:{f['line']}" + (f"-{f['end_line']}" if f["end_line"] != f["line"] else "")
        cls = f" ({f['class']})" if f.get("class") else ""
        tag = f["severity"] + (", pre-existing" if f.get("pre_existing") else "")
        votes = f" votes {f['votes']}" if f.get("votes") else ""
        lines.append(f"  {loc} [{tag}]{cls}{votes} {f['title']}")
        if f.get("failure"):
            lines.append(f"      failure: {f['failure']}")
    return lines
