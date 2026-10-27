"""diff_review - one reviewer pass over a git diff, shared by two CLIs.

`scripts/review-eval.py` scores reviewer configurations against the defect set
harvested from Fix-Note commits; `scripts/review-branch.py` reviews a yard
branch before HELM merges it. Both send the same prompt through the same seam,
so a configuration that scores well in the eval is the one the branch review
runs, not a sibling of it.

The review rules travel IN the prompt. `REVIEW.md` is read only by the managed
Code Review product, never by a local Claude Code session (found 2026-10-12),
so a rule file on disk would be a rule nobody applies.

The model call goes through `scripts/utils/proxy_transport.call_model` and
nothing else (M045.1). Which models a configuration may name is
`TRUSTED_MODELS`, the named trust list (M009.1), and anything else is refused
before a byte leaves the machine.

The diff the model sees carries the NEW-side line number of every line, so a
finding's `line` is read off the page rather than computed from hunk headers,
which is where a reviewer's line numbers usually go wrong.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
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
8. Do not repeat a finding listed under "Already reported"."""

PLAIN = """Review this change for bugs."""

OUTPUT = """Answer with ONE JSON object and nothing else:
{"count": <number of bug and security findings>,
 "summary": "<one sentence; starts with the count; says plainly when nothing needs fixing>",
 "findings": [{"file": "<path as in the diff>", "line": <new-file line>, "end_line": <new-file line>,
               "severity": "bug|security|nit", "class": "<defect class>",
               "title": "<one line>", "failure": "<input or state -> wrong outcome>"}]}"""


@dataclass(frozen=True)
class Config:
    """One reviewer configuration: a model and a prompt shape."""

    name: str
    model: str
    rules: bool = True
    max_tokens: int = 16384
    timeout: float = 600.0


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
    )
}


def is_trusted(model: str) -> bool:
    return model in TRUSTED_EXACT or model.startswith(TRUSTED_PREFIXES)


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


def raw_diff(repo: Path, base: str, head: str, paths: list[str] | None = None, context: int = 12) -> str:
    """`git diff base head`, rename-aware, restricted to `paths` when given."""
    args = ["diff", f"-U{context}", "-M", "--no-color", base, head]
    if paths is not None:
        if not paths:
            return ""
        args += ["--", *paths]
    return git(repo, *args)


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
    out: list[str] = []
    files: list[str] = []
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
                files.append(path)
                out.append(f"=== {label}")
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
    text = "\n".join(out)
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars] + "\n[... diff truncated here; nothing below this line was reviewed ...]"
    return text, files, truncated


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
        return [f for f in self.findings if f.get("severity") in BLOCKING]


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
        "title": str(f.get("title") or "").strip(),
        "failure": str(f.get("failure") or "").strip(),
    }


def review(cfg: Config, diff_text: str, intent: str, already: list[dict] | None = None) -> Review:
    """One reviewer pass. A transport or parse failure is returned, never raised."""
    if not is_trusted(cfg.model):
        raise ValueError(f"model {cfg.model!r} is not on the named trust list (M009.1)")
    from scripts.utils.proxy_transport import call_model

    prompt = build_prompt(cfg, diff_text, intent, already)
    t0 = time.monotonic()
    try:
        answer = call_model(cfg.model, prompt, temperature=0.0, max_tokens=cfg.max_tokens, timeout=cfg.timeout)
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


def est_tokens(chars: int) -> int:
    """A token ESTIMATE at four characters a token; the proxy reports no usage to this seam."""
    return round(chars / 4)


# ============================================================
# Summary, count first
# ============================================================


def summary_lines(findings: list[dict], dropped_nits: int = 0) -> list[str]:
    """The count-first summary both CLIs print."""
    block = [f for f in findings if f["severity"] in BLOCKING]
    nits = [f for f in findings if f["severity"] == "nit"]
    if not block:
        head = f"0 findings need fixing. Nothing blocks the merge. ({len(nits)} nit(s))"
    else:
        bugs = sum(1 for f in block if f["severity"] == "bug")
        sec = len(block) - bugs
        head = f"{len(block)} finding(s) need fixing before merge ({bugs} bug, {sec} security); {len(nits)} nit(s)."
    lines = [head]
    if dropped_nits:
        lines.append(f"({dropped_nits} further nit(s) dropped by the cap of {NIT_CAP})")
    order = {s: i for i, s in enumerate(("security", "bug", "nit"))}
    for f in sorted(findings, key=lambda f: (order[f["severity"]], f["file"], f["line"])):
        loc = f"{f['file']}:{f['line']}" + (f"-{f['end_line']}" if f["end_line"] != f["line"] else "")
        cls = f" ({f['class']})" if f.get("class") else ""
        lines.append(f"  {loc} [{f['severity']}]{cls} {f['title']}")
        if f.get("failure"):
            lines.append(f"      failure: {f['failure']}")
    return lines
