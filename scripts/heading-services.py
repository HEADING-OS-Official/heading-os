#!/usr/bin/env python3
"""Install, inspect and remove every HEADING OS background service through one entry point.

Usage:
    python scripts/heading-services.py list [--json]
    python scripts/heading-services.py install --all [--dry-run]
    python scripts/heading-services.py install NAME [NAME ...] [--dry-run]
    python scripts/heading-services.py status [--all | NAME ...]
    python scripts/heading-services.py remove NAME [NAME ...] [--dry-run]
    python scripts/heading-services.py render --to DIR [--platform linux|darwin] [NAME ...]

A thin CLI over the service layer in `scripts/utils/schedule.py`, which holds
the manifest of every unit family in `scripts/templates/systemd/` (HOS-303,
deployment design section 2 step 7, approved 2026-11-01). On Linux it installs
systemd user units with linger, through each family's own `scripts/install-*.sh`
where one exists; on macOS it writes launchd agents (designed in, UNTESTED on
a Mac). On Linux without a systemd user session it refuses with one sentence
instead of finishing with the services silently absent.

`--all` means the families a fresh install turns on (`mandatory` in `list`);
naming a family installs it whether or not it is mandatory. `render` with no
NAME renders every family.

install, status and remove act on the running service manager, so they run
from HELM only; `--dry-run` and `render` touch no service manager and run
anywhere. Exit codes: 0 done, 1 a family failed or is not running, 2 refused.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.clone_guard import require_main_clone  # noqa: E402
from scripts.utils.colors import RED, RESET  # noqa: E402
from scripts.utils.schedule import (  # noqa: E402
    ServiceLayerError,
    install_services,
    manifest_problems,
    manifest_rows,
    remove_services,
    render_services,
    select_families,
    service_status,
)

EXIT_OK, EXIT_FAILED, EXIT_REFUSED = 0, 1, 2


def _print_list(as_json: bool) -> None:
    rows = manifest_rows()
    if as_json:
        print(json.dumps(rows, indent=2))
        return
    for row in rows:
        mark = "mandatory" if row["mandatory"] else "optional"
        print(f"{row['name']}  [{row['kind']}, {mark}, {'/'.join(row['platforms'])}]")
        print(f"    schedule: {row['schedule']}")
        print(f"    command:  {row['command']}")
        if row["why"]:
            print(f"    why off:  {row['why']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_list = sub.add_parser("list", help="print the manifest")
    p_list.add_argument("--json", action="store_true")
    for name, text in (("install", "install and start"), ("status", "report"), ("remove", "stop and delete")):
        p = sub.add_parser(name, help=f"{text} service families")
        p.add_argument("names", nargs="*", metavar="NAME")
        p.add_argument("--all", action="store_true", help="every mandatory family")
        if name != "status":
            p.add_argument("--dry-run", action="store_true", help="print what would happen, touch nothing")
            p.add_argument("--platform", choices=("linux", "darwin"),
                           help="with --dry-run: plan for this platform instead of this machine")
    p_render = sub.add_parser("render", help="write the units or plists into a directory, install nothing")
    p_render.add_argument("names", nargs="*", metavar="NAME")
    p_render.add_argument("--to", type=Path, required=True, metavar="DIR")
    p_render.add_argument("--platform", choices=("linux", "darwin"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # This file's own checkout, never `get_workspace_root()`: that honours WORKSPACE_ROOT,
    # so a copied .env could bake another tree into units the HELM guard approved.
    workspace = Path(__file__).resolve().parent.parent
    problems = manifest_problems(workspace)
    if problems:
        for problem in problems:
            print(f"{RED}manifest:{RESET} {problem}", file=sys.stderr)
        return EXIT_REFUSED
    if args.command == "list":
        _print_list(args.json)
        return EXIT_OK

    dry_run = getattr(args, "dry_run", False)
    platform = getattr(args, "platform", None)
    if args.command in ("install", "remove", "status"):
        if args.all == bool(args.names):
            print(f"{args.command}: give --all or one or more NAMEs, not both and not neither", file=sys.stderr)
            return EXIT_REFUSED
        if platform and not dry_run:
            print("--platform plans for another machine, so it needs --dry-run", file=sys.stderr)
            return EXIT_REFUSED
        if not dry_run:
            # The units bake this checkout's path in; from a YARD they would point at a
            # worktree that is deleted after the merge.
            require_main_clone(__file__)
    try:
        if args.command == "render":
            written = render_services(select_families(args.names or None), args.to,
                                      workspace_dir=workspace, target_platform=platform)
            print(f"rendered {len(written)} file(s) into {args.to.expanduser().resolve()}")
            return EXIT_OK
        fams = select_families(args.names or None, mandatory_only=args.all)
        if args.command == "install":
            ok = install_services(fams, workspace_dir=workspace, target_platform=platform, dry_run=dry_run)
        elif args.command == "remove":
            ok = remove_services(fams, target_platform=platform, dry_run=dry_run)
        else:
            ok = service_status(fams)
    except ServiceLayerError as exc:
        print(f"{RED}refused:{RESET} {exc}", file=sys.stderr)
        return EXIT_REFUSED
    return EXIT_OK if ok else EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
