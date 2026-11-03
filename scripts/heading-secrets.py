#!/usr/bin/env python3
"""heading-secrets.py -- put a secret into `.env` without it passing through a chat.

Usage:
    .venv/bin/python scripts/heading-secrets.py set ANTHROPIC_API_KEY
    .venv/bin/python scripts/heading-secrets.py list
    .venv/bin/python scripts/heading-secrets.py check EXCHANGE_PASSWORD

`set NAME` asks for the value twice at a hidden terminal prompt and writes
`NAME=value` into the engine's `.env`, atomically and at mode 600, through the
one `.env` writer `scripts/utils/env_file.py`. The value is never echoed,
printed, logged or passed on the command line. It refuses to run without a
terminal, because without one Python's `getpass` falls back to reading the
value with echo ON.

`window NAME [NAME ...]` opens one window titled `HEADING OS` with a labelled
field per name, in the order given (HOS-312, the installer's secret window).
A name ending in PASSWORD, KEY, TOKEN or SECRET gets a hidden field and a
second hidden `<NAME> again` field; any other name one visible field. Save
checks every field first: an empty one, two hidden entries that differ, or a
line break (`ENV_LINE_BREAKERS`) shows a message in the window naming the
field, never its value, and nothing is written. Only then is each value
written, byte for byte, through the same writer as `set`. It prints only
`<NAME> saved` lines, or `Cancelled. Nothing was saved.`; exit 0 on save, 1 on
Cancel or a closed window, 2 for a bad name or a failed write (the values
written before the failure stay), 3 when no window can be opened here.

`list` prints names only: every name `.env.example` declares and every name
`.env` assigns, each marked `set` or `unset`. `check NAME` prints one such line
and exits 0 when the name is set, 1 when it is not.

Why this exists (design section 2 step 5, 2026-11-01, HOS-302): the setup
wizard asked for the Anthropic API key in the chat, so the key landed in the
session transcript. The wizard now tells the executive to run `set` in a
terminal instead.

Exit codes: 0 ok, 1 not set (`check`) or refused input or a cancelled window,
2 usage or write error, 3 no window can be opened (`window`).
"""
from __future__ import annotations

import argparse
import getpass
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.colors import GRAY, GREEN, RED, RESET, YELLOW  # noqa: E402
from scripts.utils.env_file import ENV_LINE_BREAKERS, is_valid_env_var_name, upsert_env_value  # noqa: E402
from scripts.utils.paths import iter_env_pairs  # noqa: E402
from scripts.utils.workspace import get_workspace_root  # noqa: E402

EXIT_OK = 0
EXIT_UNSET = 1
EXIT_ERROR = 2
EXIT_NO_WINDOW = 3
HIDDEN_SUFFIXES = ("PASSWORD", "KEY", "TOKEN", "SECRET")
NO_WINDOW = "No window can be opened here."
CANCELLED = "Cancelled. Nothing was saved."


def _env_path() -> Path:
    return get_workspace_root() / ".env"


def _assigned(path: Path) -> dict[str, str]:
    """Every name the file assigns, FIRST value kept, as `load_env` does."""
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for key, value in iter_env_pairs(path.read_text(encoding="utf-8")):
        out.setdefault(key, value)
    return out


def _hidden_prompt(label: str) -> str:
    """One hidden read. A missing terminal raises instead of echoing the value.

    `getpass.getpass` warns with `GetPassWarning` and then reads with echo ON
    when it cannot control the terminal. Turned into an error, the warning
    fires before the read, so no value is ever taken visibly.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        return getpass.getpass(label)


def cmd_set(name: str) -> int:
    if not is_valid_env_var_name(name):
        print(f"{RED}{name!r} is not a valid name: use A-Z, 0-9 and _, "
              f"not starting with a digit.{RESET}", file=sys.stderr)
        return EXIT_ERROR
    try:
        # Byte for byte, never stripped: a secret with a leading or trailing
        # space was written changed while the command reported success (HOS-302
        # review, 2026-11-01). Only an empty value is refused here; a line break
        # is refused by the writer.
        first = _hidden_prompt(f"Type or paste {name} (hidden), then press Enter: ")
        if not first:
            print(f"{RED}Nothing was typed. {name} is unchanged.{RESET}", file=sys.stderr)
            return EXIT_UNSET
        again = _hidden_prompt(f"Type or paste {name} again to confirm: ")
    except getpass.GetPassWarning:
        print(f"{RED}No terminal to read from, so the value cannot be hidden. "
              f"Run this command yourself in a terminal window.{RESET}", file=sys.stderr)
        return EXIT_ERROR
    except (EOFError, KeyboardInterrupt):
        print(f"\n{YELLOW}Cancelled. {name} is unchanged.{RESET}", file=sys.stderr)
        return EXIT_UNSET
    if first != again:
        print(f"{RED}The two entries differ. {name} is unchanged; run the "
              f"command again.{RESET}", file=sys.stderr)
        return EXIT_UNSET
    path = _env_path()
    try:
        upsert_env_value(path, name, first)
    except ValueError as e:
        # The message names the key and the reason, never the value.
        print(f"{RED}{e}{RESET}", file=sys.stderr)
        return EXIT_UNSET
    except OSError as e:
        print(f"{RED}Cannot write {path}: {e.strerror}{RESET}", file=sys.stderr)
        return EXIT_ERROR
    print(f"{GREEN}{name} saved to {path} (mode 600).{RESET}")
    return EXIT_OK


def _window_problem(fields: list[tuple[str, object, object]]) -> str | None:
    """The first field that cannot be saved, as a sentence naming it; never its value."""
    for name, entry, again in fields:
        value = entry.get()
        if not value:
            return f"{name} is empty."
        if again is not None and value != again.get():
            return f"The two entries for {name} differ."
        if any(ch in value for ch in ENV_LINE_BREAKERS):
            return f"{name} contains a line break; paste it again without one."
    return None


def cmd_window(names: list[str]) -> int:
    """One window with a field per name; the values go from the fields to .env, nowhere else."""
    for name in names:
        if not is_valid_env_var_name(name):
            print(f"{RED}{name!r} is not a valid name: use A-Z, 0-9 and _, "
                  f"not starting with a digit.{RESET}", file=sys.stderr)
            return EXIT_ERROR
    try:
        import tkinter
    except ImportError:
        print(NO_WINDOW, file=sys.stderr)
        return EXIT_NO_WINDOW
    try:
        root = tkinter.Tk()
    except tkinter.TclError:
        print(NO_WINDOW, file=sys.stderr)
        return EXIT_NO_WINDOW
    root.title("HEADING OS")
    fields: list[tuple[str, object, object]] = []
    row = 0
    for name in names:
        hidden = name.endswith(HIDDEN_SUFFIXES)
        tkinter.Label(root, text=name).grid(row=row, column=0, sticky="w", padx=8, pady=4)
        entry = tkinter.Entry(root, width=40, show="*" if hidden else "")
        entry.grid(row=row, column=1, padx=8, pady=4)
        row += 1
        again = None
        if hidden:
            tkinter.Label(root, text=f"{name} again").grid(row=row, column=0, sticky="w", padx=8, pady=4)
            again = tkinter.Entry(root, width=40, show="*")
            again.grid(row=row, column=1, padx=8, pady=4)
            row += 1
        fields.append((name, entry, again))
    message = tkinter.Label(root, text="", fg="red")
    message.grid(row=row, column=0, columnspan=2, padx=8, pady=4)
    outcome = {"code": EXIT_UNSET}

    def save() -> None:
        problem = _window_problem(fields)
        if problem:
            message.config(text=problem)
            return
        path = _env_path()
        try:
            for name, entry, _again in fields:
                upsert_env_value(path, name, entry.get())
                print(f"{name} saved", flush=True)
        except ValueError as e:
            # The message names the key and the reason, never the value.
            print(f"{RED}{e}{RESET}", file=sys.stderr)
            outcome["code"] = EXIT_ERROR
        except OSError as e:
            print(f"{RED}Cannot write {path}: {e.strerror}{RESET}", file=sys.stderr)
            outcome["code"] = EXIT_ERROR
        else:
            outcome["code"] = EXIT_OK
        root.destroy()

    def cancel() -> None:
        root.destroy()

    tkinter.Button(root, text="Save", command=save).grid(row=row + 1, column=0, padx=8, pady=8)
    tkinter.Button(root, text="Cancel", command=cancel).grid(row=row + 1, column=1, padx=8, pady=8)
    root.protocol("WM_DELETE_WINDOW", cancel)
    fields[0][1].focus_set()
    root.mainloop()
    if outcome["code"] == EXIT_UNSET:
        print(CANCELLED, flush=True)
    return outcome["code"]


def _status_line(name: str, assigned: dict[str, str]) -> str:
    if assigned.get(name):
        return f"{GREEN}set{RESET}    {name}"
    return f"{GRAY}unset{RESET}  {name}"


def cmd_list() -> int:
    root = get_workspace_root()
    try:
        assigned = _assigned(_env_path())
        declared = _assigned(root / ".env.example")
    except (OSError, UnicodeDecodeError) as e:
        print(f"{RED}Cannot read the .env files under {root}: {e}{RESET}", file=sys.stderr)
        return EXIT_ERROR
    names = list(declared) + [n for n in assigned if n not in declared]
    for name in names:
        print(_status_line(name, assigned))
    return EXIT_OK


def cmd_check(name: str) -> int:
    try:
        assigned = _assigned(_env_path())
    except (OSError, UnicodeDecodeError) as e:
        print(f"{RED}Cannot read {_env_path()}: {e}{RESET}", file=sys.stderr)
        return EXIT_ERROR
    print(_status_line(name, assigned))
    return EXIT_OK if assigned.get(name) else EXIT_UNSET


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Put a secret into .env from a hidden terminal prompt, "
                    "and show which names are set. Values are never printed.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_set = sub.add_parser("set", help="ask for NAME at a hidden prompt and write it to .env")
    p_set.add_argument("name")
    sub.add_parser("list", help="names only, each marked set or unset")
    p_check = sub.add_parser("check", help="exit 0 when NAME is set, 1 when it is not")
    p_check.add_argument("name")
    p_window = sub.add_parser("window", help="one window with a field per NAME; Save writes them to .env")
    p_window.add_argument("names", nargs="+", metavar="NAME")
    args = parser.parse_args(argv)
    if args.cmd == "window":
        return cmd_window(args.names)
    if args.cmd == "set":
        return cmd_set(args.name)
    if args.cmd == "list":
        return cmd_list()
    return cmd_check(args.name)


if __name__ == "__main__":
    sys.exit(main())
