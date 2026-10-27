"""Rules engine for Inbox Pulse cheap classifier.

Loads email-triage-rules.yaml from the data config directory
(`get_data_config_dir()`, resolved by the daemon) at startup and on
file-change. Provides matching helpers for sender overrides and keyword
overrides, and the internal-domain list.

Glob semantics for sender overrides: fnmatch-style wildcards.
- "newsletter@*"          matches any sender starting with "newsletter@"
- "*@linkedin.com"        matches any sender ending with "@linkedin.com"
- "alice@example.com" exact match

Sovereignty discipline
----------------------
This module does NOT log email content. It accepts addresses and subjects
as arguments and returns classification labels only. No caller-supplied
string is written to logs inside this module.

Tests: none since the 2026-09-21 test-suite split. The sovereignty rule above is
unchanged and is now held by review; a catch-all rule that the report cannot see
is the failure the parked checks were written against.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Optional

import yaml

log = logging.getLogger(__name__)

# Priority order for sender override buckets (highest first).
_SENDER_PRIORITY: list[str] = ["always_critical", "always_important", "always_normal"]

# Map sender bucket names to keyword bucket names so we can share helpers.
_KEYWORD_PRIORITY: list[str] = ["promote_to_critical", "promote_to_important"]

# How many characters of the body preview to search.
_BODY_PREVIEW_CHARS = 500


@dataclass
class RulesEngine:
    """Loads + matches against email-triage-rules.yaml in the data config directory.

    Caller passes the YAML path explicitly (for testability). On each
    reload(), the file is re-read; if missing or invalid, the engine
    keeps its last-known-good state and logs a warning.

    Empty-posture fallback
    ----------------------
    If the YAML file is missing OR fails to parse OR is empty/None, the
    engine operates with an empty config dict. Every match helper returns
    None, and `internal_domains` returns []. The daemon keeps
    running without crashing.
    """

    yaml_path: Path
    _config: dict = field(default_factory=dict, repr=False)
    _last_mtime: float = field(default=0.0, repr=False)
    _missing_warned: bool = field(default=False, repr=False)
    # The mtime of the last version of the file that failed to load. A broken
    # file keeps its mtime above _last_mtime (which only advances on success),
    # so reload_if_changed() retries every poll cycle and each retry logged the
    # full warning again -- forever, at the same rate the missing-file case was
    # throttled to avoid. Retrying is right; saying so 2,880 times a day is not.
    # Keyed on mtime, so SAVING the file warns again if it is still broken.
    _bad_load_warned_mtime: float | None = field(default=None, repr=False)
    # Buckets already reported as scalar-instead-of-list, so the warning is
    # emitted once per config rather than once per inbound email. Cleared
    # whenever a new config is accepted, so a re-saved file warns again.
    _scalar_warned: set = field(default_factory=set, repr=False)

    def __post_init__(self) -> None:
        self.reload()

    # ------------------------------------------------------------------
    # Load / reload
    # ------------------------------------------------------------------

    def reload(self) -> bool:
        """Re-read the YAML file. Return True if config changed, False if unchanged.

        On parse error or missing file the engine keeps its prior config and
        logs a warning -- it never raises. The bad-load warning is throttled by
        mtime, for the reason given on `_bad_load_warned_mtime`; the
        missing-file warning is throttled by `_missing_warned`, shared with
        `reload_if_changed` and cleared the moment the file is readable again.
        """
        try:
            mtime = self.yaml_path.stat().st_mtime
        except OSError:
            # "Warn once" was a comment with nothing behind it: no flag was set,
            # so every call warned again. `_missing_warned` already existed for
            # exactly this, used by `reload_if_changed`, which returns before it
            # ever reaches here -- so the throttle the comment promised lived in
            # the one path that could not use it. Sharing the flag gives it one
            # meaning: the operator has already been told the file is gone.
            if not self._missing_warned:
                if self._config:
                    log.warning("email-triage-rules.yaml not found at %s; keeping prior config",
                                self.yaml_path)
                else:
                    log.warning("email-triage-rules.yaml not found at %s; running with empty rules",
                                self.yaml_path)
                self._missing_warned = True
            return False

        # The file is readable again, so a future disappearance warns afresh.
        self._missing_warned = False

        try:
            raw = self.yaml_path.read_text(encoding="utf-8")
            parsed = yaml.safe_load(raw)
        except Exception as exc:  # noqa: BLE001
            if self._bad_load_warned_mtime != mtime:
                log.warning("Failed to parse %s: %s; keeping prior config",
                            self.yaml_path, exc)
                self._bad_load_warned_mtime = mtime
            return False

        if not parsed or not isinstance(parsed, dict):
            if self._bad_load_warned_mtime != mtime:
                log.warning("%s parsed as empty or non-dict; keeping prior config",
                            self.yaml_path)
                self._bad_load_warned_mtime = mtime
            return False

        # A good load clears the throttle, so the next breakage warns at once.
        self._bad_load_warned_mtime = None

        if parsed == self._config:
            # File changed mtime but content is identical (e.g., touch).
            self._last_mtime = mtime
            return False

        self._config = parsed
        self._last_mtime = mtime
        self._scalar_warned.clear()
        log.info("email-triage-rules.yaml reloaded from %s", self.yaml_path)
        return True

    def reload_if_changed(self) -> bool:
        """Check mtime; call reload() only if the file changed since last load.

        Returns True if a reload happened AND the config changed.
        Returns False if the file is unchanged or the reload produced no diff.

        If the file has disappeared since the last successful load (OSError on
        stat), logs a one-shot warning so the operator notices, then returns
        False and keeps the prior config. The warning is throttled so a missing
        file doesn't spam the log every poll cycle.
        """
        try:
            mtime = self.yaml_path.stat().st_mtime
        except OSError as exc:
            if not self._missing_warned:
                log.warning(
                    "rules YAML stat() failed (file gone?): %s -- keeping prior config",
                    exc,
                )
                self._missing_warned = True
            return False

        # File came back; reset the missing flag so a future disappearance re-warns
        self._missing_warned = False

        if mtime <= self._last_mtime:
            return False

        return self.reload()

    # ------------------------------------------------------------------
    # Bucket shape
    # ------------------------------------------------------------------

    def _mapping(self, raw: object, where: str) -> dict:
        """Return `raw` as a mapping, tolerating the two ways YAML says "nothing".

        `reload()` checks only that the YAML ROOT is a dict, so a section may
        hold anything the operator typed, and `self._config.get(key, {})` hands
        back the stored value rather than the default whenever the key is
        present with a null value. MEASURED 2026-09-01 against parseable
        configs, each one line long:

            sender_overrides:                 -> None  -> .get() AttributeError
            keyword_overrides:                -> None  -> .get() AttributeError
            sender_overrides: "*@x.example"   -> str   -> .get() AttributeError

        Every one of those raised straight out of `classify`, which the daemon
        catches and logs per message, so the whole override set stopped applying
        and every email fell through to LOW with nothing saying the rules were
        never consulted. That is the same failure the class docstring rules out
        ("the daemon keeps running without crashing").

        Warned once per config, through the same set `_pattern_list` uses, so a
        bad section does not write a line per inbound message.
        """
        if isinstance(raw, dict):
            return raw
        if raw is not None and where not in self._scalar_warned:
            log.warning(
                "%s: %s is a %s, not a mapping; ignoring it. Write it as a "
                "YAML mapping to silence this.",
                self.yaml_path,
                where,
                type(raw).__name__,
            )
            self._scalar_warned.add(where)
        return {}

    def _pattern_list(self, raw: object, where: str) -> list:
        """Return `raw` as a list of pattern strings, tolerating a bare scalar.

        `reload()` checks only that the YAML root is a dict, so a bucket may
        hold anything the operator typed. The natural single-entry form is a
        scalar, `always_critical: "*@acme-telecom.example"`, and iterating a
        string yields its CHARACTERS. Measured 2026-08-29 on exactly that
        config: the first character is `*`, `fnmatch(addr, "*")` is true for
        every address, and `match_sender("nobody@unrelated.example")` returned
        "always_critical". A scalar keyword "DUE" degraded to the
        single-character test `"d" in haystack`, which matched "lunch tomorrow".
        The config parses cleanly, so nothing anywhere said a word about it.

        Wrapping the scalar reads it the way the operator meant it, and the
        warning tells them the file wants a list.
        """
        if raw is None:
            return []
        if isinstance(raw, list):
            # Every entry is stringified, not just the bare-scalar case below.
            # A pattern reaches `fnmatch` and `.lower()`, and YAML types an
            # unquoted entry natively: MEASURED 2026-09-01,
            # `always_critical: [31337]` raised `AttributeError: 'int' object
            # has no attribute 'lower'` out of `match_sender`.
            return [str(p) for p in raw if p is not None]
        if where not in self._scalar_warned:
            log.warning(
                "%s: %s is a single value, not a list; reading it as a "
                "one-entry list. Write it as a YAML list to silence this.",
                self.yaml_path,
                where,
            )
            self._scalar_warned.add(where)
        return [str(raw)]

    # ------------------------------------------------------------------
    # Sender matching
    # ------------------------------------------------------------------

    def match_sender(self, email_addr: str) -> Optional[str]:
        """Return 'always_critical' | 'always_important' | 'always_normal' | None.

        Glob match: case-insensitive. First-match-wins in priority order
        (always_critical > always_important > always_normal).
        """
        sender_overrides = self._mapping(
            self._config.get("sender_overrides"), "sender_overrides")
        addr_lower = email_addr.lower()

        for bucket in _SENDER_PRIORITY:
            patterns = self._pattern_list(
                sender_overrides.get(bucket), f"sender_overrides.{bucket}"
            )
            for pattern in patterns:
                if fnmatch(addr_lower, pattern.lower()):
                    return bucket

        return None

    # ------------------------------------------------------------------
    # Keyword matching
    # ------------------------------------------------------------------

    def match_keywords(self, subject: str, body_preview: str = "") -> Optional[str]:
        """Return 'promote_to_critical' | 'promote_to_important' | None.

        Case-insensitive substring match against subject + first 500 chars
        of body_preview. promote_to_critical wins over promote_to_important.
        """
        keyword_overrides = self._mapping(
            self._config.get("keyword_overrides"), "keyword_overrides")
        haystack = (subject + " " + body_preview[:_BODY_PREVIEW_CHARS]).lower()

        for bucket in _KEYWORD_PRIORITY:
            keywords = self._pattern_list(
                keyword_overrides.get(bucket), f"keyword_overrides.{bucket}"
            )
            for kw in keywords:
                if kw.lower() in haystack:
                    return bucket

        return None

    # ------------------------------------------------------------------
    # Internal domains
    # ------------------------------------------------------------------

    @property
    def internal_domains(self) -> list[str]:
        """List of internal email domains for the recipient-aware classifier rule.

        Used by CheapClassifier to determine whether a sender is internal
        (own company) -- internal senders get the `tribe-leadership` + To/CC
        short-circuit logic; external senders go through the standard
        7-signal classifier.

        Defaults to [] if not configured (which effectively disables the
        recipient-aware rule).
        """
        return self._pattern_list(
            self._config.get("internal_domains"), "internal_domains")
