#!/usr/bin/env python3
"""Save the clipboard image as `clip.png` under the registry name `clipboard-grabs`.

Usage:
    python scripts/clip.py

Reads the current clipboard via PIL.ImageGrab. On Linux with older Pillow or
when no X11/Wayland clipboard helper is available via PIL, falls back to
shelling out to xclip / wl-paste directly. Exits with status 1 if no image is
on the clipboard. Prints the absolute path of the saved PNG on success.
"""
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.venv_guard import ensure_venv  # noqa: E402
from scripts.utils.data_path import data_write_dir  # noqa: E402


def _grab_via_cli(out_path: Path, argv: list[str]) -> bool:
    """Linux fallback: run `argv` (xclip or wl-paste) with stdout into FILE.

    Every failing exit removes the file this call created: `open("wb")`
    truncates before the grabber starts and the grabber is killed at
    `timeout=5`, so a slow grab would otherwise leave a truncated PNG at the
    well-known path. The timeout is a `SubprocessError`, a failure path here.
    """
    if not shutil.which(argv[0]):
        return False
    try:
        with out_path.open("wb") as f:
            result = subprocess.run(
                argv, stdout=f, stderr=subprocess.DEVNULL, timeout=5,
            )
    except (subprocess.SubprocessError, OSError):
        out_path.unlink(missing_ok=True)
        return False
    if result.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0:
        return True
    out_path.unlink(missing_ok=True)
    return False


# Wayland first, then X11.
_GRABBERS = (
    ["wl-paste", "--type", "image/png"],
    ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
)


def main() -> int:
    # The guard runs HERE and not at module level, because this file's name has
    # no hyphen, so `import scripts.clip` works, and a module-level call would
    # re-exec or edit the environment of whatever process imported it. That is
    # the shape `scripts/dev/import-env-purity.py` refuses (HOS-116).
    ensure_venv()
    # After the guard, so a bare system interpreter re-execs into the venv first (2026-08-24).
    from PIL import ImageGrab

    # The last grab only, overwritten on every run: `tmp` kind, runtime junk.
    out = data_write_dir("clipboard-grabs") / "clip.png"

    # `grabclipboard()` does not return None when the platform has no clipboard
    # grabber. It RAISES, and the raise happens before `img` is ever bound, so
    # every branch below it — including the Linux fallback, the one part of this
    # file that could still find an image — was unreachable BY CONSTRUCTION on
    # such a host. Measured 2026-08-30 on WSL2 with Pillow 12.3.0 and neither
    # binary installed:
    #     NotImplementedError: wl-paste or xclip is required for
    #     ImageGrab.grabclipboard() on Linux
    # The operator saw a raw traceback. Catching it HERE, at the entry, is what
    # lets the rest of the function run at all; patching a later branch could
    # not, because control never reached one.
    #
    # It is not only the "nothing installed" host. PIL takes wl-paste only for a
    # wayland session and xclip only for an x11 one (PIL/ImageGrab.py), so a host
    # with WAYLAND_DISPLAY set and ONLY xclip installed also lands here — and for
    # that host the xclip fallback below succeeds where PIL refused to try.
    #
    # NotImplementedError alone. Anything else from PIL is genuinely unexpected
    # and must keep its traceback.
    no_grabber = ""
    try:
        img = ImageGrab.grabclipboard()
    except NotImplementedError as exc:
        img = None
        no_grabber = str(exc)
    # `grabclipboard()` returns THREE shapes: an Image, a LIST of file paths
    # (documented behaviour on Windows and macOS when files rather than image
    # data are on the clipboard), or None. `is not None` accepted the list and
    # `img.save` then raised AttributeError on a traceback. Copying a file in
    # the OS file manager is an entirely ordinary thing to do before running
    # this.
    if isinstance(img, list):
        print(f"Clipboard holds {len(img)} file path(s), not an image. "
              f"Copy the image itself, or open the file directly:",
              file=sys.stderr)
        for entry in img[:5]:
            print(f"  {entry}", file=sys.stderr)
        return 1
    if img is not None:
        img.save(str(out), "PNG")
        print(str(out))
        return 0

    # PIL found nothing, or refused to look. On Linux try the direct CLI
    # fallbacks before giving up.
    if sys.platform.startswith("linux"):
        for argv in _GRABBERS:
            if _grab_via_cli(out, argv):
                print(str(out))
                return 0

    if no_grabber:
        # PIL's own words for the reason, so this sentence cannot drift away from
        # what the library actually checked.
        print(f"Cannot read the clipboard: {no_grabber}; install wl-clipboard "
              f"or xclip, then copy the image and run this again.",
              file=sys.stderr)
    else:
        print("No image on clipboard.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
