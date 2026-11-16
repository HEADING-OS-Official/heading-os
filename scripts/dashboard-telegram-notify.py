#!/usr/bin/env python3
"""dashboard-telegram notify: timer entrypoint that sends today's morning dashboard to Telegram.

Thin orchestrator (no LLM, no state of its own). On each fire it:
  1. runs `scripts/generate-dashboard.py`, which writes today's
     `morning-dashboard.html` into the dated leaf under the `dashboard`
     registry name,
  2. strips hidden characters from that HTML (scripts/utils/sanitize_text.py)
     and rescans it,
  3. sends the HTML as a Telegram document to the operator's own sink through
     the notifications bot (`telegram_notify.notify_file`), captioned with
     three counts parsed from the generator's stdout.

The operator asked for this delivery, so its absence is a failure: any failure
(generator exit, timeout or spawn error, unreadable file, hidden characters
left after the rewrite, a refused send, no recipient) exits 1 and the oneshot
unit reads `failed`. Each failure with a recipient also sends ONE fixed-label
text line, `Dashboard YYYY-MM-DD not delivered: <label>`, carrying no path,
stderr or exception text; the detail goes only to the local log. This departs
from ops-radar-notify.py, which exits 0 on a failed send.

The recipient is read from the gitignored engine `.env`, never hardcoded:
    OPS_RADAR_TELEGRAM_TARGET -> ODIN_CADENCE_TELEGRAM_TARGET -> unconfigured (exit 1)

`--dry-run` builds the dashboard into a fresh directory under this checkout's
scratch (kept, its path printed), so the live dashboard directory is never
written, runs steps 1-2, and sends nothing: it prints whether a recipient is
present (never its value), whether it is one of the operator's own targets,
the attachment refusal check, file size, sanitizer counts and the caption.

Invoked by scripts/templates/systemd/dashboard-telegram.service (daily timer,
08:00 in the configured timezone). By hand, the safe form:
    python3 scripts/dashboard-telegram-notify.py --dry-run
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# Workspace import bootstrap (per development-standards.md)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.workspace import get_workspace_root, make_run_clock  # noqa: E402
from scripts.utils.paths import load_env  # noqa: E402
from scripts.utils import telegram_notify  # noqa: E402
from scripts.utils.venv_guard import venv_python  # noqa: E402
from scripts.utils.data_path import DataPathError, checkout_scratch_dir, data_path  # noqa: E402
from scripts.utils.sanitize_text import sanitize_report, scan_file  # noqa: E402
from scripts.utils.atomic import atomic_write_text  # noqa: E402
from scripts.utils.sensitivity import publish_refusal  # noqa: E402

GENERATOR = "scripts/generate-dashboard.py"
HTML_NAME = "morning-dashboard.html"
GENERATOR_TIMEOUT = 600

# The generator's own summary lines (scripts/generate-dashboard.py main()).
# A pattern that does not match puts "?" in the caption; it is not a failure.
RED_RE = re.compile(r"CRM: \d+ contacts \((\d+) red")
DEALS_RE = re.compile(r"Pipeline: (\d+) deals")
MEETINGS_RE = re.compile(r"Calendar: (\d+) meetings today")


class Failure(Exception):
    """A run that did not deliver; `label` is the only text that leaves the machine."""

    def __init__(self, label: str) -> None:
        super().__init__(label)
        self.label = label


def _log(msg: str) -> None:
    print(f"[dashboard-telegram-notify] {msg}", file=sys.stderr)


def _count(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(text)
    return match.group(1) if match else "?"


def _caption(day: str, stdout: str) -> str:
    return (f"Dashboard {day}: {_count(RED_RE, stdout)} red contacts, "
            f"{_count(DEALS_RE, stdout)} deals, {_count(MEETINGS_RE, stdout)} meetings today")


def _generate(root: Path, out_dir: Path | None) -> str:
    """Run the generator; return its stdout. `out_dir` is passed only in dry-run."""
    cmd = [str(venv_python()), str(root / GENERATOR)]
    if out_dir is not None:
        cmd += ["--output-dir", str(out_dir)]
    try:
        proc = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True,
                              timeout=GENERATOR_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        _log(f"generator did not finish in {GENERATOR_TIMEOUT}s: {exc}")
        raise Failure("generator timeout") from exc
    except OSError as exc:
        _log(f"generator could not start ({type(exc).__name__}: {exc})")
        raise Failure("generator could not start") from exc
    if proc.returncode != 0:
        _log(f"generator exited {proc.returncode}: {(proc.stderr or '').strip()[-500:]}")
        raise Failure(f"generator exit {proc.returncode}")
    return proc.stdout or ""


def _clean_html(html_path: Path) -> tuple[int, int]:
    """Strip hidden characters in place; return (removed, replaced). Raises Failure."""
    if not html_path.is_file():
        _log(f"dashboard HTML absent: {html_path}")
        raise Failure("file unreadable")
    try:
        text = html_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        _log(f"dashboard HTML unreadable ({type(exc).__name__}: {exc})")
        raise Failure("file unreadable") from exc
    clean, removed, replaced = sanitize_report(text)
    if removed or replaced:
        try:
            atomic_write_text(html_path, clean)
        except OSError as exc:
            _log(f"dashboard HTML could not be rewritten ({type(exc).__name__}: {exc})")
            raise Failure("file unreadable") from exc
        _log(f"hidden characters: {removed} removed, {replaced} replaced")
    remaining, report = scan_file(html_path)
    if remaining:
        _log(f"{remaining} hidden characters remain after the rewrite: {report[:500]}")
        raise Failure("hidden characters remain")
    return removed, replaced


def _report_failure(day: str, recipient: str, label: str, dry_run: bool) -> int:
    line = f"Dashboard {day} not delivered: {label}"
    if dry_run:
        print(line)
        return 1
    _log(line)
    if recipient and not telegram_notify.notify(recipient, line):
        _log("failure line not delivered either (see telegram_notify log)")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Send today's morning-dashboard HTML to the operator's own Telegram sink.")
    parser.add_argument("--dry-run", action="store_true",
                        help="build into checkout scratch and report; send nothing")
    args = parser.parse_args()

    root = get_workspace_root()
    load_env(root)  # make .env (the recipient variables) visible under systemd too
    today = make_run_clock()().date()
    day = today.strftime("%Y-%m-%d")

    recipient = (os.environ.get("OPS_RADAR_TELEGRAM_TARGET")
                 or os.environ.get("ODIN_CADENCE_TELEGRAM_TARGET")
                 or "")
    if not recipient and not args.dry_run:
        _log("no recipient configured; nothing sent")
        return 1

    try:
        if args.dry_run:
            try:
                out_dir = Path(tempfile.mkdtemp(prefix="dashboard-telegram-dry-",
                                                dir=checkout_scratch_dir()))
            except (DataPathError, OSError) as exc:
                _log(f"dry-run scratch directory unavailable ({type(exc).__name__}: {exc})")
                raise Failure("generator could not start") from exc
            print(f"dry-run output directory: {out_dir}")
            stdout = _generate(root, out_dir)
        else:
            try:
                out_dir = data_path("dashboard") / day
            except DataPathError as exc:
                _log(f"dashboard directory unresolvable ({exc})")
                raise Failure("file unreadable") from exc
            stdout = _generate(root, None)
        html_path = (out_dir / HTML_NAME).resolve()
        removed, replaced = _clean_html(html_path)
    except Failure as failure:
        return _report_failure(day, recipient, failure.label, args.dry_run)

    caption = _caption(day, stdout)

    if args.dry_run:
        own = bool(recipient) and recipient in telegram_notify.own_targets()
        refusal = publish_refusal(html_path)
        try:
            size = html_path.stat().st_size
        except OSError as exc:
            _log(f"dashboard HTML stat failed ({type(exc).__name__}: {exc})")
            return _report_failure(day, recipient, "file unreadable", True)
        print(f"recipient present: {'yes' if recipient else 'no'}")
        print(f"recipient is an own target: {'yes' if own else 'no'}")
        print(f"publish_refusal: {refusal}")
        print(f"file size: {size} bytes")
        print(f"hidden characters: {removed} removed, {replaced} replaced")
        print(f"caption: {caption}")
        return 0 if (recipient and own and refusal is None) else 1

    if telegram_notify.notify_file(recipient, str(html_path), caption):
        _log(f"dashboard {day} delivered")
        return 0
    _log("notify_file refused or failed (see telegram_notify log)")
    return _report_failure(day, recipient, "send refused", False)


if __name__ == "__main__":
    sys.exit(main())
