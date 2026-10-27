#!/usr/bin/env python3
"""The instruction-injection vocabulary, in one place.

Three consumers read this one list: the advisory PostToolUse hook
`.claude/hooks/prompt-guard.py`, which watches text written INTO the data ingest
paths; `scripts/harness-audit.py`, which watches text this workspace INSTALLS and
then loads into every session; and `scripts/utils/untrusted_input.py`, which
STRIPS markers from external text before it enters a prompt. Detection reads
every entry outside `STRIP_ONLY_CATEGORIES`; stripping applies only the entries
that carry a replacement.

Do not split this list (decided 2026-10-16, when the strip list was merged in):
none of the three consumers blocks anything, so none needs an embedded copy,
and one definition cannot drift from itself.

The patterns are deliberately shallow. Anyone who reads them can phrase around
them, and this module does not pretend otherwise: it catches text nobody read,
not a targeted adversary. The part of the harness audit that would catch a
targeted payload is the drift check, which flags a CHANGE regardless of wording.
"""
import re

# (compiled_regex, category, replacement). Detection reports the first entry
# that matches a line, in this order. `replacement` is what
# `untrusted_input.sanitize_untrusted` puts in place of a match, applied in this
# order; None means detect only, because the phrase also occurs in ordinary
# prose and stripping it would mangle benign text.
INJECTION_PATTERNS = [
    # Classic injection
    (re.compile(r'ignore\s+(all\s+)?(previous|above)\s+instructions', re.I),
     "classic-injection", None),
    (re.compile(r'disregard\s+(all\s+)?previous', re.I),
     "classic-injection", None),
    (re.compile(r'forget\s+(all\s+)?(your\s+)?instructions', re.I),
     "classic-injection", None),
    (re.compile(r'override\s+(system|previous)\s+(prompt|instructions)', re.I),
     "classic-injection", None),

    # Role manipulation
    (re.compile(r'you\s+are\s+now\s+(?:a|an|the)\s+', re.I),
     "role-manipulation", None),
    (re.compile(r'pretend\s+(?:you(?:\'re| are)\s+|to\s+be\s+)', re.I),
     "role-manipulation", None),
    (re.compile(r'from\s+now\s+on,?\s+you\s+(?:are|will|should|must)', re.I),
     "role-manipulation", None),

    # System prompt extraction
    (re.compile(
        r'(?:print|output|reveal|show|display|repeat)\s+'
        r'(?:your\s+)?(?:system\s+)?(?:prompt|instructions)', re.I),
     "prompt-extraction", None),

    # Fake markup
    (re.compile(r'</?(?:system|assistant|human)>', re.I),
     "fake-markup", None),
    (re.compile(r'\[SYSTEM\]'),
     "fake-markup", None),
    (re.compile(r'\[INST\]'),
     "fake-markup", None),
    (re.compile(r'<<\s*SYS\s*>>'),
     "fake-markup", None),

    # Invisible Unicode (injection markers)
    (re.compile(r'[\u200B-\u200F\u2028-\u202F\uFEFF\u00AD]'),
     "invisible-unicode", None),

    # The strip vocabulary of `untrusted_input.py`, merged here 2026-10-16.
    # Syntactically meaningless in normal prose, so each is replaced; more
    # specific first, because the replacements apply in order.
    (re.compile(r"(?im)^\s*(?:system|assistant|user)\s*:"),
     "role-marker", "[ROLE_STRIPPED]"),
    (re.compile(r"(?i)ignore\s+(?:all\s+)?(?:previous|above|prior)\s+(?:instructions?|prompts?|context)"),
     "classic-injection", "[INSTR_STRIPPED]"),
    (re.compile(r"(?i)<\|im_(?:start|end)\|>(?:system|user|assistant)?"),
     "fake-markup", "[MARKER_STRIPPED]"),
    (re.compile(r"(?i)\[\[/?INST\]\]"),
     "fake-markup", "[MARKER_STRIPPED]"),
    (re.compile(r"(?i)(?:disregard|forget|override|bypass)\s+(?:all\s+)?(?:previous\s+)?(?:rules?|instructions?|constraints?|safety)"),
     "classic-injection", "[INSTR_STRIPPED]"),
    (re.compile(r"(?i)you\s+are\s+now\s+in\s+\w+\s+mode"),
     "role-manipulation", "[PERSONA_STRIPPED]"),
    (re.compile(r"(?i)(?:output|send|email|forward|exfiltrate)\s+(?:all\s+)?(?:crm|contacts?|calendar|passwords?|secrets?)"),
     "exfiltration", "[EXFIL_STRIPPED]"),
    # The frames `untrusted_input.wrap_untrusted` draws. Untrusted text carrying
    # a forged closing frame placed its next sentence outside the frame
    # (MEASURED 2026-08-26 for the dash frame); the shape is stripped, so
    # guessing the label does not help. The tag frame (2026-09-27) is primarily
    # defended by its per-call random id.
    (re.compile(r"-{3,}\s*\[[^\]\n]{0,120}\]\s*-{3,}"),
     "fake-frame", "[DELIM_STRIPPED]"),
    (re.compile(r"(?i)</?\s*external_content\b[^>\n]{0,200}>?"),
     "fake-frame", "[TAG_STRIPPED]"),
]

#: The entries `untrusted_input.sanitize_untrusted` applies, in order.
STRIP_PATTERNS = [(rx, repl) for rx, _cat, repl in INJECTION_PATTERNS if repl is not None]

#: Stripped from untrusted text but not REPORTED by `scan_content`: a `User:` line
#: and "email contact" are ordinary in this workspace's own skills. MEASURED
#: 2026-10-16, reporting them took `harness-audit.py` from 0 findings to 11, all
#: in documentation (`scrutinize` transcripts, an `osint` search line, a
#: `deep-think` example).
STRIP_ONLY_CATEGORIES = frozenset({"role-marker", "exfiltration"})


def scan_content(text):
    """Every injected line in `text`, as `(line_number, snippet, category)`.

    The snippet is bounded and exists for a human reading a warning in their own
    terminal. A caller that PERSISTS a finding must record the category and drop
    the snippet: an audit that copies a payload into a tracked artifact has moved
    it closer to the operator, not further away.
    """
    if not text:
        return []

    findings = []
    for line_num, line in enumerate(text.split("\n"), start=1):
        for pattern, category, _replacement in INJECTION_PATTERNS:
            if category in STRIP_ONLY_CATEGORIES:
                continue
            match = pattern.search(line)
            if match:
                start = max(0, match.start() - 10)
                end = min(len(line), match.end() + 10)
                snippet = line[start:end].strip()
                if len(snippet) > 60:
                    snippet = snippet[:57] + "..."
                findings.append((line_num, snippet, category))
                break  # One finding per line is enough.
    return findings
