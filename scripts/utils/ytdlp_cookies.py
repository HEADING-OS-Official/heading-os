#!/usr/bin/env python3
"""Parse a BROWSER[:PROFILE] spec into yt-dlp's `cookiesfrombrowser` tuple.

Usage:
    from scripts.utils.ytdlp_cookies import parse_browser_spec
    spec = parse_browser_spec("brave:Default")   # ("brave", "Default", None, None)

Shared by the playwright skill (`pw.py youtube --browser`) and the yt-pulse
skill (`pulse.py --browser`), which each carried their own copy.
"""
from __future__ import annotations


def parse_browser_spec(spec: str | None) -> tuple | None:
    """Return the yt-dlp tuple, or None when `spec` is empty or `none` (opt-out)."""
    if not spec or spec.strip().lower() == "none":
        return None
    browser, _, profile = spec.partition(":")
    return (browser.strip().lower(), profile.strip() or None, None, None)
