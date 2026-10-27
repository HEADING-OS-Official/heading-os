#!/usr/bin/env python3
"""Crop one region of an image, optionally upscale it, and write it to scratch.

A skill that reads a dense visual (a chart, a diagram, a table rendered as an
image) sees small values more precisely when it can zoom into the part that
holds them. Anthropic's Opus 5.5 prompting guidance names a cropping tool as the
cheap form of that help. This is the terminal form: crop the region, scale it
up, and read the file it prints. `/docparse` renders the page with
`scripts/docparse.py page-image` and zooms with this.

The box is given as fractions of the image by default, so the same numbers work
at any render resolution (dpi): `--box 0.5 0 1 0.5` is the top-right quarter. `--units pixel`
takes pixel coordinates instead.

Usage:
    python scripts/crop-image.py page.png --box 0.5 0 1 0.5
    python scripts/crop-image.py page.png --box 0.1 0.6 0.9 0.95 --scale 3
    python scripts/crop-image.py page.png --box 120 400 900 780 --units pixel --output zoom.png

Prints the absolute path of the file it wrote. The default destination is this
checkout's scratch directory (`python scripts/data-path.py scratch`), under
`crops/`.

Exit codes:
  0  written.
  1  the image cannot be read or the output cannot be written.
  2  bad usage: a box outside the image, an empty box, or a scale out of range.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, UnidentifiedImageError  # noqa: E402

from scripts.utils.colors import RED, RESET  # noqa: E402
from scripts.utils.data_path import checkout_scratch_dir  # noqa: E402

MAX_SCALE = 8.0


def pixel_box(size: tuple[int, int], box: list[float], units: str) -> tuple[int, int, int, int]:
    """The crop box in whole pixels, or ValueError naming what is wrong."""
    width, height = size
    x0, y0, x1, y1 = box
    if units == "fraction":
        if not all(0.0 <= v <= 1.0 for v in box):
            raise ValueError(f"fractions must lie between 0 and 1: {box}")
        x0, x1 = x0 * width, x1 * width
        y0, y1 = y0 * height, y1 * height
    left, top, right, bottom = round(x0), round(y0), round(x1), round(y1)
    if not (0 <= left < right <= width and 0 <= top < bottom <= height):
        raise ValueError(f"box {left},{top},{right},{bottom} is empty or outside "
                         f"the {width}x{height} image")
    return left, top, right, bottom


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Crop one region of an image, optionally upscale it, and print the path written.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("image", help="Image file to crop (PNG, JPEG, and other formats Pillow reads)")
    parser.add_argument("--box", nargs=4, type=float, required=True,
                        metavar=("LEFT", "TOP", "RIGHT", "BOTTOM"),
                        help="Region to keep, as fractions of the image (default) or pixels")
    parser.add_argument("--units", choices=("fraction", "pixel"), default="fraction",
                        help="How to read --box (default: fraction)")
    parser.add_argument("--scale", type=float, default=1.0,
                        help=f"Upscale factor for the crop, 1 to {MAX_SCALE:g} (default: 1)")
    parser.add_argument("--output", default=None,
                        help="Output PNG path (default: this checkout's scratch, under crops/)")
    args = parser.parse_args()

    if not 1.0 <= args.scale <= MAX_SCALE:
        print(f"{RED}ERROR{RESET}  --scale must be between 1 and {MAX_SCALE:g}: {args.scale}",
              file=sys.stderr)
        return 2

    src = Path(args.image).resolve()
    try:
        img = Image.open(src)
        img.load()
    except (OSError, UnidentifiedImageError) as exc:
        print(f"{RED}ERROR{RESET}  Cannot read image {src}: {exc}", file=sys.stderr)
        return 1

    try:
        box = pixel_box(img.size, args.box, args.units)
    except ValueError as exc:
        print(f"{RED}ERROR{RESET}  {exc}", file=sys.stderr)
        return 2

    crop = img.crop(box)
    if args.scale != 1.0:
        crop = crop.resize((round(crop.width * args.scale), round(crop.height * args.scale)),
                           Image.Resampling.LANCZOS)

    if args.output:
        out = Path(args.output).resolve()
    else:
        out = checkout_scratch_dir() / "crops" / f"{src.stem}-crop-{'-'.join(map(str, box))}.png"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        crop.save(out, format="PNG")
    except OSError as exc:
        print(f"{RED}ERROR{RESET}  Cannot write {out}: {exc}", file=sys.stderr)
        return 1
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
