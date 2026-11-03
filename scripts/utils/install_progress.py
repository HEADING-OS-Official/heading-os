#!/usr/bin/env python3
"""The progress window of the unattended install run (HOS-336).

Claude starts every installer run as a background command, so while a long run
goes on the chat shows nothing that moves. On a desktop the installer therefore
shows one small window of its own: "Installing HEADING OS", step N of the total,
the current step's title, and two bars, one that advances per step and one that
keeps moving while a step runs.

The installer process exits at every stop (need-answer, waiting-for-person) and
the next run resumes, so the window never belongs to one run. The step loop of
scripts/heading-install.py writes a small progress file; one detached window
process, started through `install_person.start_detached` like a sign-in, reads
it twice a second. A run that finds the window still open reuses it.

  <target>/.heading-install/progress.json   mode 600, written atomically
      state  running | done | failed | need-answer | waiting-for-person | paused | stopped
      step   1-based index of the current step (0 before the first)
      total  number of steps
      title  the step's `@step` title
      say    only for done: the handoff sentence; never anything else
      consent  the groups of changes the person agreed to, in words ("everything"
               for all; "" before an answer), shown under the heading (HOS-340)
      pid, pid_start   the installer run that wrote it, so a run that vanished
                       without an end state is told from one still working
      seq    a stamp (time.time_ns) for this record
  <target>/.heading-install/progress-shown.json   written by the window, mode 600:
      seq    the stamp of the record it has drawn, so the installer can wait
             until an announcement is on screen before it opens what it announced
  <target>/.heading-install/control.json   mode 600, written by `request_control`
      request  pause | stop: the run ends after the step it is running (HOS-340)
      at       when it was asked (ISO time, UTC)
      Written by `heading-install.py --control` and by the window's two buttons;
      the run removes it when it reads it, and whenever its step loop ends.

The file never holds a secret, an answer or a sign-in code: a waiting-for-person
`say` (which can hold a one-time code) and a failure message (which can hold a
command's output) are never written to it; the window shows fixed sentences.

The window never blocks or slows the run: it is a separate process, it is never
made topmost and never takes focus by itself, so a secret or sign-in window
opened later stands above it. Any failure to open it prints one note and the
install carries on; without a display there is no window and the per-step lines
the installer prints are the progress.

Standard library only, like scripts/heading-install.py, which imports it at
start-up before the engine's environment exists.

Usage (the installer starts it; by hand only to look at a progress file):
    python scripts/utils/install_progress.py window <progress.json> [<pid> <pid_start>]
      <pid> <pid_start>: the run that started it, watched until a record names its writer
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.utils import install_person as person  # noqa: E402 - stdlib only
from scripts.utils.atomic import atomic_write_text  # noqa: E402 - stdlib only

PROGRESS_NAME = "progress.json"
SHOWN_NAME = "progress-shown.json"
CONTROL_NAME = "control.json"
CONTROL_REQUESTS = ("pause", "stop")
SHOWN_WAIT_S = 2.0           # how long an announcement waits to be drawn before the handoff opens anyway
WINDOW = "progress-window"
END_STATES = ("done", "failed", "need-answer", "waiting-for-person", "paused", "stopped")
POLL_MS = 500
DONE_CLOSE_S = 120           # a finished install's window closes itself after two minutes
IDLE_CLOSE_S = 60 * 60       # an end state nobody reran for an hour: the window closes itself
TITLE = "Installing HEADING OS"

_started_here = False        # this run started the window (not reused from an earlier run)
_noted = False               # the one note about a window that is gone has been printed


def progress_file(ctx) -> Path:
    return person.state_dir(ctx) / PROGRESS_NAME


def shown_file(ctx) -> Path:
    return person.state_dir(ctx) / SHOWN_NAME


def control_file(ctx) -> Path:
    return person.state_dir(ctx) / CONTROL_NAME


def request_control(ctx_or_state_dir, request: str) -> Path:
    """Ask the running install to pause or stop after the step it is running; the one writer of control.json.

    `ctx_or_state_dir` is the run's context, or the `<target>/.heading-install`
    folder itself (the window and `heading-install.py --control`, which must not
    create that folder). Returns the file written.
    """
    if request not in CONTROL_REQUESTS:
        raise ValueError(f"unknown control request {request!r}")
    folder = ctx_or_state_dir if isinstance(ctx_or_state_dir, Path) else person.state_dir(ctx_or_state_dir)
    path = folder / CONTROL_NAME
    atomic_write_text(path, json.dumps({"request": request,
                                        "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}) + "\n",
                      mode=0o600)
    return path


def _claim_control(ctx) -> Path | None:
    """Move control.json to a name only this run uses; the claimed copy, or None when there is none.

    The rename is atomic, so a request written after it lands in a new control.json and is
    read at the next step boundary; a read followed by an unlink could delete it unread.
    """
    path = control_file(ctx)
    claimed = path.with_name(f"{CONTROL_NAME}.{os.getpid()}.claimed")
    try:
        os.rename(path, claimed)
    except FileNotFoundError:
        return None
    return claimed


def take_control(ctx) -> str | None:
    """The pending request, pause or stop, claimed and removed as it is read; None when there is none.

    An unreadable file is removed too and read as no request.
    """
    claimed = _claim_control(ctx)
    if claimed is None:
        return None
    data = read(claimed)
    claimed.unlink(missing_ok=True)
    request = (data or {}).get("request")
    return request if request in CONTROL_REQUESTS else None


def clear_control(ctx) -> None:
    """Remove a request nobody consumed, so it is never carried into a later run."""
    claimed = _claim_control(ctx)
    if claimed is not None:
        claimed.unlink(missing_ok=True)


def write(ctx, state: str, step: int, total: int, title: str, say: str = "", *, consent: str = "") -> int | None:
    """Record where the run is and return the record's stamp; a failure to write is one note, never a stop."""
    if ctx.dry_run:
        return None
    seq = time.time_ns()
    payload = {"state": state, "step": step, "total": total, "title": title,
               "say": say if state == "done" else "", "consent": consent,
               "pid": os.getpid(), "pid_start": person._start_time(os.getpid()) or "", "seq": seq}
    try:
        atomic_write_text(progress_file(ctx), json.dumps(payload) + "\n", mode=0o600)
    except OSError as exc:
        print(f"  note: the progress file could not be written ({exc}); the install carries on")
        return None
    _check_window(ctx)
    return seq


def wait_shown(ctx, seq: int | None, timeout_s: float = SHOWN_WAIT_S) -> bool:
    """Wait until the window has drawn record `seq`, at most `timeout_s`; True once it has.

    No window running, or no record written: nothing to wait for, False at once.
    """
    if seq is None or ctx.dry_run or not person.running(ctx, WINDOW):
        return False
    deadline = time.monotonic() + timeout_s
    while True:
        drawn = read(shown_file(ctx))
        if drawn is not None and isinstance(drawn.get("seq"), int) and drawn["seq"] >= seq:
            return True
        if time.monotonic() >= deadline or not person.running(ctx, WINDOW):
            print(f"  note: the progress window did not show the last message within {timeout_s:g} s")
            return False
        time.sleep(0.05)


def _check_window(ctx) -> None:
    """One note when the window this run started is gone before the end."""
    global _noted
    if _started_here and not _noted and not person.running(ctx, WINDOW):
        _noted = True
        tail = person.log_tail(ctx, WINDOW, 3).strip()
        print("  note: the progress window closed or could not open"
              + (f" ({tail.splitlines()[-1]})" if tail else "") + "; the install carries on")


def _interpreter(ctx) -> str:
    """The engine's Python once it exists; before that the installer's own.

    The window uses the standard library only (tkinter included). Until the
    python-env step has run there is no `.venv`, and the installer itself runs
    on the uv Python 3.11 install.sh chose (`uv run --python 3.11`), the same
    build the `.venv` is made from.
    """
    venv = getattr(ctx, "python", None)
    return str(venv) if venv and os.access(venv, os.X_OK) else sys.executable


def start(ctx) -> None:
    """Open the window, or reuse the one an earlier run opened; never raises."""
    global _started_here
    if ctx.dry_run:
        return
    if not person.has_display():
        print("  note: no desktop here, so no progress window; the step lines below show the progress")
        return
    try:
        if person.running(ctx, WINDOW):
            return
        person.start_detached(ctx, WINDOW, [_interpreter(ctx), str(Path(__file__).resolve()), "window",
                                            str(progress_file(ctx)), str(os.getpid()),
                                            person._start_time(os.getpid()) or ""],
                              env=dict(os.environ), cwd=ctx.target)
    except OSError as exc:
        print(f"  note: the progress window could not be opened ({exc}); the install carries on")
        return
    _started_here = True


# ============================================================
# The window: what it says for each state
# ============================================================

def read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _alive(pid, pid_start) -> bool:
    """The process `pid` runs and started at `pid_start`, so a reused pid is not taken for it."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    return person.alive_since(pid, str(pid_start or ""))  # /proc on Linux, ps on macOS (HOS-341)


def _writer_alive(data: dict) -> bool:
    return _alive(data.get("pid"), data.get("pid_start"))


def view(data: dict | None, writer_alive: bool = True) -> dict:
    """The window's text for one progress record: heading, consent, step line, detail, and whether it is finished."""
    v = _view(data, writer_alive)
    agreed = str((data or {}).get("consent") or "")
    v["consent"] = f"You agreed to: {agreed}" if agreed else ""
    return v


def _view(data: dict | None, writer_alive: bool) -> dict:
    if data is None and not writer_alive:
        return {"heading": "The install stopped", "line": "",
                "detail": "The installer ended before it reported a step. Claude has the details in the chat.",
                "end": True}
    if data is None:
        return {"heading": TITLE, "line": "Starting...", "detail": "", "end": False}
    state, step, total = data.get("state"), int(data.get("step") or 0), int(data.get("total") or 0)
    title = str(data.get("title") or "")
    line = f"Step {step} of {total}" if step else ""
    if state not in END_STATES and not writer_alive:  # running, or a state this window does not know
        return {"heading": "The install stopped",
                "line": f"Stopped at step {step} of {total}: {title}" if step else "",
                "detail": "The installer ended without saying why. Claude has the details in the chat.",
                "end": True}
    if state == "running":
        return {"heading": TITLE, "line": line, "detail": title, "end": False}
    if state == "done":
        return {"heading": "HEADING OS is installed", "line": f"All {total} steps done",
                "detail": str(data.get("say") or ""), "end": True}
    if state == "failed":
        return {"heading": "The install stopped",
                "line": f"Stopped at step {step}: {title}" if step else "",
                "detail": "Claude has the details in the chat.", "end": True}
    if state == "need-answer":
        return {"heading": TITLE, "line": line,
                "detail": "Waiting for you in the chat: Claude has a question.", "end": True}
    if state == "paused":
        return {"heading": "The install is paused",
                "line": f"Paused after step {step} of {total}: {title}" if step else "Paused before step 1",
                "detail": "Nothing is broken: the finished steps stay in place. "
                          "To continue, tell Claude in the chat.", "end": True}
    if state == "stopped":
        return {"heading": "The install stopped",
                "line": f"Stopped after step {step} of {total}: {title}" if step else "Stopped before step 1",
                "detail": "You asked to stop. The finished steps stay in place; "
                          "to finish the install later, tell Claude in the chat.", "end": True}
    if state == "waiting-for-person":
        return {"heading": TITLE, "line": line,
                "detail": f"{title}\nWaiting for you in the window or browser page that opened; "
                          "then tell Claude in the chat that you are done.", "end": True}
    return {"heading": TITLE, "line": line, "detail": title, "end": False}


def run_window(path: Path, writer: tuple[str, str] | None = None) -> int:
    """The window over `path`; `writer` is the run that started it, watched until a record names its own."""
    try:
        import tkinter
        from tkinter import ttk
    except ImportError as exc:
        print(f"no progress window: tkinter is missing ({exc})", flush=True)
        return 3
    try:
        root = tkinter.Tk()
    except tkinter.TclError as exc:
        print(f"no progress window: {exc}", flush=True)
        return 3
    root.title(TITLE)
    root.resizable(False, False)
    if root.tk.call("tk", "windowingsystem") == "x11":
        root.attributes("-type", "utility")  # a small tool window, not a main one; X11 alone has -type
    heading = tkinter.Label(root, text=TITLE, font=("TkDefaultFont", 13, "bold"))
    heading.pack(padx=16, pady=(14, 4), anchor="w")
    agreed = tkinter.Label(root, text="", wraplength=420, justify="left")
    agreed.pack(padx=16, anchor="w")
    line = tkinter.Label(root, text="Starting...")
    line.pack(padx=16, anchor="w")
    detail = tkinter.Label(root, text="", wraplength=420, justify="left")
    detail.pack(padx=16, pady=(2, 8), anchor="w")
    steps_bar = ttk.Progressbar(root, length=420, mode="determinate")
    steps_bar.pack(padx=16, pady=2)
    busy = ttk.Progressbar(root, length=420, mode="indeterminate")
    busy.pack(padx=16, pady=(2, 10))
    seen = {"text": None, "changed": time.monotonic(), "end": None, "mtime": None, "after": None,
            "writer": writer, "seq": None}
    shown = path.with_name(SHOWN_NAME)

    def finish() -> None:
        """Close with no timer left behind: a pending tick or bar step would report a destroyed window."""
        if seen["after"] is not None:
            root.after_cancel(seen["after"])
        busy.stop()
        root.destroy()

    close = tkinter.Button(root, text="Close", command=finish)
    controls = tkinter.Frame(root)
    asked = {"text": ""}  # "Pause requested" or "Stop requested" while this run goes on

    def ask(request: str) -> None:
        try:
            request_control(path.parent, request)
        except OSError as exc:
            asked["text"] = f"The request could not be written ({exc})"
            return
        asked["text"] = "Pause requested" if request == "pause" else "Stop requested"
        pause_button.config(state="disabled")
        stop_button.config(state="disabled")

    pause_button = tkinter.Button(controls, text="Pause after this step", command=lambda: ask("pause"))
    stop_button = tkinter.Button(controls, text="Stop after this step", command=lambda: ask("stop"))
    pause_button.pack(side="left", padx=(0, 8))
    stop_button.pack(side="left")

    def tick() -> None:
        data = read(path)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = None
        if mtime != seen["mtime"]:
            seen["mtime"], seen["changed"] = mtime, time.monotonic()
        if data:
            seen["writer"] = (data.get("pid"), data.get("pid_start"))
        # No record at all (a first run killed before its first write, or every write failing):
        # the run that started the window is the writer to watch.
        v = view(data, _alive(*seen["writer"]) if seen["writer"] else True)
        running = not v["end"] and (data or {}).get("state") == "running"
        if not running and asked["text"]:
            asked["text"] = ""  # the run ended: the next run starts with both buttons again
            pause_button.config(state="normal")
            stop_button.config(state="normal")
        text = (v["heading"], v["consent"], v["line"], asked["text"] or v["detail"], running)
        if text != seen["text"]:
            seen["text"] = text
            heading.config(text=v["heading"])
            agreed.config(text=v["consent"])
            line.config(text=v["line"])
            detail.config(text=asked["text"] or v["detail"])
            if running:
                controls.pack(padx=16, pady=(0, 12), anchor="w")
            else:
                controls.pack_forget()
        if data:
            steps_bar.config(maximum=max(int(data.get("total") or 1), 1), value=int(data.get("step") or 0))
        if v["end"] != seen["end"]:
            seen["end"] = v["end"]
            if v["end"]:
                busy.stop()
                close.pack(pady=(0, 12))
            else:
                close.pack_forget()
                busy.start(15)
        seq = (data or {}).get("seq")
        if seq != seen["seq"]:
            root.update_idletasks()  # drawn now, before the installer is told it is on screen
            try:
                atomic_write_text(shown, json.dumps({"seq": seq}) + "\n", mode=0o600)
            except OSError as exc:
                print(f"the drawn record could not be recorded ({exc})", flush=True)
            seen["seq"] = seq
        idle = time.monotonic() - seen["changed"]
        if v["end"] and ((data or {}).get("state") == "done" and idle >= DONE_CLOSE_S or idle >= IDLE_CLOSE_S):
            seen["after"] = None
            finish()
            return
        seen["after"] = root.after(POLL_MS, tick)

    root.protocol("WM_DELETE_WINDOW", finish)
    tick()
    root.mainloop()
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) not in (2, 4) or argv[0] != "window":
        print(__doc__.split("Usage", 1)[1].strip(), file=sys.stderr)
        return 2
    return run_window(Path(argv[1]), (argv[2], argv[3]) if len(argv) == 4 else None)


if __name__ == "__main__":
    sys.exit(main())
