#!/usr/bin/env python3
"""Credential patterns, and the two operations over them.

The single source of truth for what this workspace calls a secret. The wall that
imports it is scripts/secret-scanner.py (push time and commit time); the other
importers call `redact` below to strip a credential from text they write (a
handoff, a pin, a log line). `git grep -n "secret_patterns import"` lists them.

One consumer does NOT import it, on purpose. .claude/hooks/_dispatch.py is
the blocking PreToolUse gate, and its own comments record why its one external
import is lazy and guarded: any exception from that import would take down the
whole PreToolUse chain, secret detection included, on every tool call. That
degradation is safe for the Canopus deny, whose guarantee lives elsewhere. It
would not be safe here, because at that layer secret detection IS the guarantee,
and a guarded import falling back to an empty list is a fail-open hole in a
security gate. So _dispatch.py keeps an embedded copy, and a test held the two
in lockstep until the 2026-09-21 test-suite split. Nothing compares them now,
so an edit to either vocabulary has to be made in both by hand.

That guard is not theoretical. The copies had already drifted twice before it
existed: once on the {16,} threshold (fixed by hand under F-L4) and once on the
placeholder lookahead of the environment-password entry, found AND closed on
2026-07-31 by parsing both files.
"""
import re

# Inline allowlist token (same convention as Yelp/detect-secrets). A line carrying
# this marker is an intentional, reviewed pattern (test fixtures, docs) and is skipped.
ALLOWLIST_TOKEN = "pragma: allowlist secret"

# Secret patterns: (compiled_regex, description)
# Thresholds tuned to avoid matching placeholders like "sk-ant-your-key-here"
#
# EVERY PREFIXED ENTRY OPENS WITH `(?<![A-Za-z0-9_-])`, and the reason is a
# measurement rather than symmetry. A credential is a STANDALONE token: it sits
# after an equals sign, a quote, a colon, whitespace, or at the start of a line.
# A prefix preceded by another token character is not a credential, it is the
# middle of a longer run, and a long enough run of the right alphabet spells any
# short prefix by accident.
#
# MEASURED 2026-09-05 against generated base64URL payloads, which add `-` and
# `_` to the alphabet and are what a JWT, a URL-safe blob or a signed URL is
# made of:
#
#     100,000 chars     r8_ 1,  fc- 1                       ->  0
#   1,000,000 chars     r8_ 3,  fc- 1,  gho_ 1              ->  0
#  10,000,000 chars     r8_ 23, fc- 26                      ->  0
#  40,000,000 chars     r8_ 94, fc- 97, cpx- 1, ghp_ 1, gho_ 2  ->  0
#
# `fc-` is three characters, so a run spells it about once per 260,000
# positions; the {16,} tail then costs almost nothing because the run is made of
# exactly those characters. This is the same defect the AWS entry had, one
# alphabet over, and it was left unfixed on the first pass because it had only
# been reasoned about and not measured. Reasoning put it at "no instance
# observed"; measuring put it at two findings in 100 KB.
#
# `+` and `/` are deliberately NOT in this lookbehind, unlike the AWS entry's.
# They buy nothing here: every prefix above needs a `-` or a `_`, and standard
# base64 has neither, so only a base64url run can spell one. Excluding a
# character that cannot precede the defect would only cost true positives, and
# a token at the end of a URL path (`.../ghp_...`) is a shape worth keeping.
#
# WHAT REMAINS, stated rather than implied: a prefix at the very START of a
# payload, where the character before it is the delimiter (`base64,ghp_...`).
# The lookbehind passes there because a comma is not a token character. It needs
# the accident to land on the first four characters of the blob rather than
# anywhere in it, which is the difference between 97 hits in 40 MB and none.
# THE PROPOSAL TO MOVE FOUR OF THESE RULES INTO THE PRIVATE OVERLAY WAS
# EXAMINED ON 2026-09-21 AND DECLINED, and the reasoning is here so it is not
# reopened from the same starting point. The four were `pplx-`, `r8_`, `fc-`
# and the `EXCHANGE_PASSWORD` alternative below, on the argument that they are
# workspace-specific and so disclose which services the operator runs.
#
# MEASURED that day, over tracked files, excluding this module: Firecrawl is
# named in 41 of them, Perplexity in 36, Replicate in 22 and EXCHANGE_PASSWORD
# in 16. Whatever those four rules disclose, this repository already discloses
# many times over, and it has to: the scripts that CALL those services live
# here. Removing the rules would hide the detector and leave the subject.
#
# The three prefixes are also not facts about this workspace at all. They are
# the vendors' own published token formats, documented on the vendors' own
# sites, and they say exactly as much about the operator as `ghp_` and
# `sk-ant-` do, which is nothing. `EXCHANGE_PASSWORD` sits beside `DB_PASSWORD`,
# `SMTP_PASSWORD` and `AUTH_PASSWORD`; it is an environment variable name.
#
# The cost side decides it. A rule list arriving from the overlay is a dynamic
# input, and a dynamic input can arrive EMPTY. That is not hypothetical here:
# `scripts/content-guard.py`'s own docstring records its denylist arriving empty
# on a clone with no overlay, the gate exiting 0 printing "denylist unavailable;
# skipped", and all 92 tests over it passing. This module is read by the commit
# hook AND by the unbypassable push-time scan, so an empty list there is a push
# wall that waves everything through, on exactly the clone least likely to
# notice. The cheapest way not to have that defect is not to have the input.
SECRET_PATTERNS = [
    # API key formats - require 16+ chars of key material after prefix (aligned with _dispatch.py, F-L4)
    (re.compile(r'(?<![A-Za-z0-9_-])sk-ant-[a-zA-Z0-9_-]{16,}'), "Anthropic API key"),
    (re.compile(r'(?<![A-Za-z0-9_-])pplx-[a-zA-Z0-9]{16,}'), "Perplexity API key"),
    (re.compile(r'(?<![A-Za-z0-9_-])r8_[a-zA-Z0-9]{16,}'), "Replicate API token"),
    (re.compile(r'(?<![A-Za-z0-9_-])fc-[A-Za-z0-9]{16,}'), "Firecrawl API key"),
    (re.compile(r'(?<![A-Za-z0-9_-])ctx7sk-[a-zA-Z0-9-]{16,}'), "Context7 API key"),
    (re.compile(r'(?<![A-Za-z0-9_-])cpx-[a-zA-Z0-9]{16,}'), "CLIProxyAPI local proxy key"),
    (re.compile(r'(?<![A-Za-z0-9_-])ghp_[a-zA-Z0-9]{16,}'), "GitHub personal access token"),
    (re.compile(r'(?<![A-Za-z0-9_-])gho_[a-zA-Z0-9]{16,}'), "GitHub OAuth token"),
    # THE BOUNDARY IS THE PATTERN, and without it this entry stops pushes over
    # credentials that are not there. An AWS access key ID is a STANDALONE
    # twenty-character token: it sits after an equals sign, inside quotes, on its
    # own line. It is never adjacent to more base64 characters on both sides,
    # while `AKIA` followed by sixteen uppercase-or-digit characters is a run a
    # base64 payload spells by accident. MEASURED 2026-09-05 on the refusal that
    # blocked a real push: eight matches across two generated HTML files, every
    # one of them inside an embedded image, e.g. `...AAAAAAAAAIUC` before and
    # `CwWBUFQAAVBY` after. Zero were tokens.
    #
    # `AKIA` is the only entry a STANDARD base64 run can spell: every other
    # bare-literal prefix carries a `-`, a `_` or a `.`, and standard base64 has
    # none of the three.
    #
    # base64URL adds `-` and `_`, which is why every other entry carries its own
    # lookbehind; the header comment above them carries the numbers.
    #
    # This entry keeps `+/` in its own boundary and the others do not, because
    # `AKIA` is the one prefix standard base64 reaches.
    #
    # THE PADDING CHARACTER IS DELIBERATELY NOT IN THE LOOKAHEAD, against the
    # first instinct, because it can only lose matches and cannot gain one. For
    # `=` to sit immediately after this match inside a base64 payload, the run
    # would have to start exactly at `AKIA` and be exactly twenty characters
    # long, and twenty is a multiple of four, so well-formed base64 of that
    # length carries no padding at all. The case cannot arise. What `=` in the
    # lookahead WOULD do is drop a real key written immediately before one, so it
    # is left out: this is a security wall, and a rule that only ever subtracts
    # matches earns its place by measurement or not at all.
    (re.compile(r'(?<![A-Za-z0-9+/])AKIA[0-9A-Z]{16}(?![A-Za-z0-9+/])'), "AWS access key"),
    (re.compile(r'(?<![A-Za-z0-9_-])xoxb-[0-9]+-[a-zA-Z0-9]+'), "Slack bot token"),
    (re.compile(r'(?<![A-Za-z0-9_-])xoxp-[0-9]+-[a-zA-Z0-9]+'), "Slack user token"),
    (re.compile(r'(?<![A-Za-z0-9_-])ya29\.[A-Za-z0-9._-]{50,}'), "Google OAuth token"),
    # JWT, PEM private keys, and credentialed connection strings (F-L3; mirror in _dispatch.py)
    (re.compile(r'(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}'), "JWT bearer token"),
    # THE BOUNDARY, and it is the AKIA argument one alphabet over. A PEM header
    # is a STANDALONE line: it begins a line, or follows whitespace, a quote or
    # an escape, and a line break follows it. Until 2026-09-22 this entry
    # carried no boundary at all, alone among the entries above, so the literal
    # fired wherever it sat - including in the middle of a run of token
    # characters, which is a shape no key can be written as and a shape a
    # machine-generated listing quoting a sample produces easily.
    #
    # `-` IS DELIBERATELY NOT IN EITHER LOOKAROUND, against the reflex to copy
    # the sibling entries' `(?<![A-Za-z0-9_-])`. This literal's own delimiter is
    # a run of dashes, so a dash on either side is a malformed header rather
    # than a token continuation, and excluding it would drop a header written
    # with six dashes instead of five. This is a wall: a rule that only ever
    # subtracts matches earns its place by measurement, and that one does not.
    (re.compile(r'(?<![A-Za-z0-9_])-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----(?![A-Za-z0-9_])'), "PEM private key"),
    # The userinfo halves exclude the characters a URI cannot carry unencoded
    # (" < > { } | \ ^ ` [ ]). Without that, JSON reads as a credential: a URL
    # value, the closing quote, a comma, the next key's quote, and a JSON-LD
    # `@` key satisfy `host:user@` exactly. MEASURED 2026-09-17, this refused a
    # push over `"location":"http://www.example-company.ca","member":{"@type"` in a
    # scraped page whose content was already on the remote, and refused the
    # writing of the very test file that reproduces it. Both directions after
    # the change: 8 credentialed URLs still fire, that line and five other
    # shapes go silent, and the file went 1 hit to 0.
    (re.compile(r'[a-zA-Z][a-zA-Z0-9+.-]{0,31}://(?!user:pass(?:word)?@|username:password@)[^:@\s/?"<>{}|\\^`\[\]]{2,}:[^:@\s/?"<>{}|\\^`\[\]]{2,}@'), "connection string with inline credentials"),
    # Markdown password fields with actual values (not placeholders).
    #
    # CASE IS IGNORED ON FOUR ALTERNATIVES AND ONLY FOUR: the ones that end in a
    # SPACE, `See `, `Set `, `Use `, `Your `. Until 2026-09-27 the whole
    # exclusion was case-sensitive, so an instruction written in lower case, a
    # label followed by "set one and remember it" or by "your GitHub personal
    # access token", read as a password. MEASURED 2026-09-27 on runtime-built
    # text through every consumer of this list (scanner, push wall, redactor,
    # the hook's Bash and Write walls): those two lines and their upper-case
    # forms fired everywhere before the change and are silent after it, while a
    # real-looking value still fires everywhere.
    #
    # WHY THIS CANNOT ADMIT A SECRET THE OLD FORM REFUSED, except one shape
    # named here. Each of the four requires an English word followed by a
    # SPACE, so a value is excluded only when its first token is exactly that
    # word; a one-token value that merely begins with those letters ("setZ...",
    # "user...") still fires, because the space is part of the alternative. The
    # residual is a PASSPHRASE whose first word is one of the four in some case
    # other than the capitalised one; the capitalised form of that same
    # passphrase was already excluded, so this is the old residual in more
    # spellings, not a new class.
    #
    # THE OTHER SIX STAY CASE-SENSITIVE ON PURPOSE. `Stored`, `REDACTED`, `N/A`,
    # `TBD`, `Change` and `Reset` carry no trailing space, so a case-insensitive
    # form would exclude any value that merely BEGINS with those letters, and
    # lower-case "changeme..." or "reset2024..." is what a real weak password
    # looks like. Flagging the whole lookahead would have been that loosening.
    # The same text sits in `.claude/hooks/_dispatch.py`; keep it byte-identical.
    (re.compile(
        r'\*\*Password:\*\*\s+'
        r'(?!Stored|REDACTED|N/A|TBD|Change|Reset|(?i:See |Set |Use |Your ))'
        r'[^\n]{8,}'
    ), "Plaintext password in markdown"),
    # Generic env-style password assignments with real values.
    #
    # The exclusion is WHOLE-VALUE. Until 2026-07-31 it read
    # `(?!(?i:your[-_]|changeme|example|placeholder|redacted|dummy|xxx|<))`,
    # which tested only how the value BEGINS, so any tail could ride in behind a
    # marker: a real password starting "xxx" passed this pattern, the blocking
    # PreToolUse gate, AND the push-time content scan this workspace calls
    # unbypassable. All seven word alternatives were defeated identically.
    #
    # Now the exclusion fires only when the value TAKEN WHOLE has placeholder
    # shape: words of letters, each with an optional short digit tail, joined by
    # - or _, carrying a marker somewhere. A real password breaks that shape at
    # the first letter/digit mix inside a word, with or without punctuation.
    #
    # Three things to inherit rather than rediscover:
    #   - The old `<` alternative was inert and is gone. `<` is not in the value
    #     class, so a value starting with it never reached the exclusion at all.
    #   - Residual FALSE NEGATIVE, like the {0,31} bound above: a real password
    #     that is itself word-shaped and carries a marker
    #     ("dummy-horse-battery-staple") is still excluded. Strictly smaller than
    #     the old hole, which admitted any tail whatsoever.
    #   - Residual FALSE POSITIVE, the direction that actually reaches an
    #     operator: a PLACEHOLDER that breaks word shape is now flagged, e.g.
    #     "changeme123!" (trailing symbol) or "your-P4ssw0rd" (digits inside a
    #     word). Such a value cannot be told apart from a real password, so
    #     flagging is the safe direction, but it lands as a hard refusal at BOTH
    #     the write gate and the push wall, and it composes with the same-day
    #     removal of `.env.example`'s path exemption. If a template needs one of
    #     these shapes, write the placeholder in word shape. A pure-digit token
    #     IS word shape ("placeholder-secret-1", "dummy_value_123"): those are
    #     ordinary template values and admitting them costs the guarantee
    #     nothing, measured — all 14 bypass samples stay flagged either way.
    #
    # Measured before shipping, across both repositories: 5089 files, 1327584
    # lines, 0 new positives, 0 regressions.
    (re.compile(
        r'(?:EXCHANGE_PASSWORD|DB_PASSWORD|SMTP_PASSWORD|AUTH_PASSWORD)'
        r'\s*=\s*'
        r'(?i:(?!'
        r'(?=[A-Za-z0-9_-]*(?:your|changeme|example|placeholder|redacted|dummy|x{3,}))'
        r'(?:[A-Za-z]+[0-9]{0,3}|[0-9]{1,4})'
        r'(?:[-_](?:[A-Za-z]+[0-9]{0,3}|[0-9]{1,4}))*'
        r'(?![A-Za-z0-9!@#$%^&*_+=-])'
        r'))'
        r'[A-Za-z0-9!@#$%^&*_+=-]{8,}'
    ), "Password in environment variable assignment"),
]


# A pattern that CANNOT match without some literal substring records it here.
# Testing for the substring first is O(n) and changes no verdict; it only avoids
# the quadratic start-position scan the regex engine performs otherwise.
#
# One entry, because one pattern was measured pathological at the pre-impl gate.
# The connection-string pattern opened with [a-zA-Z][a-zA-Z0-9+.-]*:// , giving
# the engine no literal to anchor on, so it retried at every start position:
#
#     12,500 chars  0.14s      50,000 chars   1.79s
#     25,000 chars  0.46s     100,000 chars   7.21s
#                             200,000 chars  32.20s
#
# Doubling the input quadrupled the time. A possessive quantifier was tried and
# REJECTED by measurement: 1.5x, still quadratic. The cost is the start-position
# scan, not backtracking.
#
# THE GUARD ALONE WAS NOT ENOUGH, and saying otherwise here was wrong until
# 2026-07-31. It skips the pattern only when "://" is ABSENT, and "://" appears
# in essentially every real markdown file, so realistic input paid the quadratic
# in full. That was LIVE: check_prevent_secrets in .claude/hooks/_dispatch.py
# passes whole file content as ONE string, the PreToolUse timeout is 30 seconds,
# and a timed-out hook returns no decision, which the harness reads as no
# objection. The blocking secret gate failed OPEN on a large enough Write.
#
# The run before "://" is now BOUNDED to {0,31}. A URI scheme is short by
# definition (RFC 3986: ALPHA *( ALPHA / DIGIT / "+" / "-" / "." ), and the
# longest registered scheme is nowhere near 32), and a match starting inside a
# longer run usually still fires because the engine also tries later start
# positions. Measured on an unbroken 400,000-character run: unbounded did not
# finish inside two minutes, bounded takes 0.035s. Verdicts were differenced
# across every tracked file in BOTH repositories, 5,067 files and 1,194,834
# lines: ZERO disagreements.
#
# ONE CLASS IS GENUINELY LOST, and saying "not a coverage change" here was
# wrong. The first character class is [a-zA-Z] while the run is [a-zA-Z0-9+.-],
# so a later start position must land on an ASCII LETTER. When 32 or more
# consecutive characters drawn from [0-9+.-] sit immediately before "://" and
# the nearest letter is further back than that, there is no legal later start
# and the match is gone. Measured shape, described rather than written out
# because writing it out is a credential-shaped literal the gate refuses: a
# hyphenated word, then thirty-six digits, then a scheme separator and an
# ordinary userinfo pair. Bounded returns no match where unbounded returned one.
# Every ordinary scheme is
# unaffected, which is why the 5,067-file differential is silent, and no
# realistic file was found with this shape. It is written down because a wall's
# comment claiming a stronger property than it holds is how the next author
# decides not to re-check.
#
# The guard stays, because it is still free and still O(n) on needle-free text:
#
#   needle ABSENT   200,000 chars 0.00015s   1,000,000 chars 0.00068s
#   needle PRESENT  400,000 chars 0.03477s   (bounded; unbounded did not finish)
#
# The other SIXTEEN patterns open with fixed text, so the engine already anchors
# them and none needs an entry. Counted 2026-08-26: 17 patterns, 1 entry here,
# 16 others. The comment said fifteen, which is the kind of number the next
# author reads instead of re-counting, and this file is the one place where a
# wrong claim about coverage costs a credential.
#
# "Fixed text" rather than "a literal", also counted rather than assumed:
# fourteen of the sixteen open with a bare literal prefix (`sk-ant-`, `AKIA`,
# `xoxb-`, `-----BEGIN `, and so on). The remaining two open with fixed text
# that is not a bare literal - the markdown-password pattern with the escaped
# `**Password:**`, and the env-var pattern with an alternation of fixed variable
# names - so both still give the engine something to anchor on.
#
# Adding an entry that is not logically exact would silently disable a pattern,
# so entries are added only with a measurement.
REQUIRED_SUBSTRING = {
    "connection string with inline credentials": "://",
}


def iter_patterns(text: str):
    """Yield (pattern, description) for every pattern that could match `text`.

    The single place the prefilter lives, so a consumer cannot forget it. Both
    the scanner and the redactor iterate this rather than SECRET_PATTERNS.
    """
    for pattern, description in SECRET_PATTERNS:
        needle = REQUIRED_SUBSTRING.get(description)
        if needle is not None and needle not in text:
            continue
        yield pattern, description


REDACTION_TEMPLATE = "[REDACTED: {description}]"


def _redact_segment(segment: str) -> str:
    """Redact one segment: every span, computed on the ORIGINAL text, merged.

    Both halves of that sentence are load-bearing, and the second is the one
    that was missing. Substituting pattern by pattern is DESTRUCTIVE across
    patterns: the marker carries a space and a bracket, so redacting an inner
    span breaks the character-class run an enclosing pattern depends on, and the
    enclosing pattern then matches nothing. Measured on an API key sitting where
    a connection string's username goes - the password after the colon walked
    out intact, and the wall accepted it, because the broken shape no longer
    matched anything at all. A fixpoint loop does not help: that output is
    already a fixpoint.

    So every span is collected first, overlapping spans are merged into one
    interval, and each interval is replaced in a single right-to-left pass. A
    merged interval is named after the WIDEST pattern that contributed to it,
    which for a nested credential is the enclosing one.
    """
    spans = []
    for pattern, description in iter_patterns(segment):
        for match in pattern.finditer(segment):
            start, end = match.span()
            if end > start:
                spans.append((start, end, description))
    if not spans:
        return segment

    spans.sort(key=lambda span: (span[0], -span[1]))
    merged = []
    for start, end, description in spans:
        if merged and start < merged[-1][1]:
            prev_start, prev_end, prev_description, prev_width = merged[-1]
            if end - start > prev_width:
                prev_description, prev_width = description, end - start
            merged[-1] = (prev_start, max(prev_end, end), prev_description, prev_width)
        else:
            merged.append((start, end, description, end - start))

    out = []
    cursor = 0
    for start, end, description, _width in merged:
        out.append(segment[cursor:start])
        out.append(REDACTION_TEMPLATE.format(description=description))
        cursor = end
    out.append(segment[cursor:])
    return "".join(out)


def redact(text: str) -> str:
    """Replace every credential-shaped SPAN with a marker naming what it was.

    "The redactor's output passes the scanner" rests on four things, none of
    them the allowlist:

      1. The same pattern vocabulary, iterated through the same `iter_patterns`
         prefilter, over the same line boundaries scan_file reads.
      2. Every span computed on the original segment and merged before any
         substitution, so no substitution can disarm a sibling pattern and no
         partial residue is left behind (see `_redact_segment`).
      3. No marker text containing anything the vocabulary matches. The
         prefilter half of that was pinned by a test until the 2026-09-21
         test-suite split; check a new marker against `iter_patterns` yourself.
      4. Measurement rather than argument: a differential fuzz nests every
         credential family inside every container family in both orders and
         checks that nothing the vocabulary covered in the input survives.

    THE ALLOWLIST IS DELIBERATELY NOT HONOURED HERE, and that is a difference
    from scan_file, not an oversight. `ALLOWLIST_TOKEN` marks a human-authored,
    reviewed pattern in a SOURCE file. A compact summary is machine-generated
    and reviewed by nobody, and the marker lands in one by accident - most
    likely in a session ABOUT the secret scanner, which is exactly the session
    shape that caused the incident this redactor exists for. Honouring it there
    let a live credential through the redactor AND the wall together. Ignoring
    it only ever redacts MORE, so the invariant above is strictly stronger for
    it: whatever the scanner would flag is a subset of what this removes.

    The span is replaced rather than the line, so the caller's prose survives:
    the handoff exists to let the next session resume, and a gutted summary
    fails at that.

    ONE PATTERN IS AN EXCEPTION, and it is stated here rather than discovered
    later. "Plaintext password in markdown" ends in [^\\n]{8,}, which is greedy
    to end of line, so a line beginning with a bolded Password label loses
    everything after that label, prose included. Measured: a sentence about a
    rotation policy came back as the marker alone. The pattern is doing its job,
    since text after that label is exactly where a password would sit, and
    narrowing it here would weaken detection to protect prose. A test pinned
    that greedy behaviour as intended; the 2026-09-21 test-suite split took it,
    so this paragraph is what stops someone tightening the pattern.

    The redactor shares that rule's exclusion as well as its greed, so since
    2026-09-27 a label followed by an instruction in ANY case of "See", "Set",
    "Use" or "Your" plus a space is left whole, where before only the
    capitalised spelling was. That is the one change to this function's output,
    and it is confined to lines the scanner no longer flags either, so the
    subset invariant above still holds. The reasoning, and why the other six
    exclusions stayed case-sensitive, is at the pattern itself.

    Returns the input unchanged, byte for byte, when nothing matches.
    """
    if not text:
        return text

    out = []
    # split("\n"), NOT splitlines(). str.splitlines() over-splits on eight
    # code points readlines() does not treat as a break (vertical tab, form
    # feed, the three ASCII file/group/record separators, NEL, and the
    # Unicode LINE and PARAGRAPH SEPARATOR), so it cannot be used here: a
    # credential spanning one of those would be cut in two by the redactor,
    # left unmatched, and then caught by the scanner, which is the exact
    # failure this slice exists to remove.
    #
    # split("\n") alone still under-splits relative to readlines(), which
    # (via universal newlines) also treats a lone "\r" as a line break. So each
    # "\n"-segment is split again on "\r" and redacted per sub-segment, which
    # reproduces readlines()'s line boundaries exactly. That matters for prose,
    # not for safety: the markdown-password pattern runs greedily to end of
    # LINE, so on an under-split segment it would eat every "\r"-separated line
    # after the label too. Sub-segments rejoin with "\r" and lines with "\n",
    # which restores the input byte for byte.
    for line in text.split("\n"):
        out.append("\r".join(_redact_segment(sub_line) for sub_line in line.split("\r")))
    return "\n".join(out)
