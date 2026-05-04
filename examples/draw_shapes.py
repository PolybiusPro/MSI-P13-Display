#!/usr/bin/env python3
"""Draw basic graphics on the ArtInChip USB display.

Examples:

    python3 draw_shapes.py --mode circle
    python3 draw_shapes.py --mode square
    python3 draw_shapes.py --mode gradient
    python3 draw_shapes.py --mode all --frames 5

The script uses a conservative JPEG encoder setting that was stable on the
tested eM3499-Monitor: quality=60, chroma subsampling=2, chunk_size=4096.
"""

from __future__ import annotations

import argparse
import time

from PIL import Image, ImageDraw

from em3499_monitor.display import ArtInChipDisplay, PID, VID, print_platform_hints


def gradient_background(width: int, height: int) -> Image.Image:
    """Create a horizontal blue/pink gradient using direct pixel writes."""

    img = Image.new("RGB", (width, height))
    pixels = img.load()
    for y in range(height):
        for x in range(width):
            t = x / max(1, width - 1)
            v = y / max(1, height - 1)
            pixels[x, y] = (
                int(20 + 190 * t),
                int(30 + 80 * (1.0 - v)),
                int(70 + 160 * (1.0 - t)),
            )
    return img


def draw_circle(width: int, height: int) -> Image.Image:
    """Draw a filled circle centered on a dark background."""

    img = Image.new("RGB", (width, height), (8, 11, 18))
    draw = ImageDraw.Draw(img)
    radius = min(width, height) // 4
    cx, cy = width // 2, height // 2
    draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=(0, 210, 255))
    draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), outline=(255, 255, 255), width=6)
    return img


def draw_square(width: int, height: int) -> Image.Image:
    """Draw a filled square centered on a dark background."""

    img = Image.new("RGB", (width, height), (8, 11, 18))
    draw = ImageDraw.Draw(img)
    side = min(width, height) // 2
    left = (width - side) // 2
    top = (height - side) // 2
    draw.rectangle((left, top, left + side, top + side), fill=(255, 196, 0), outline=(255, 255, 255), width=6)
    return img


def draw_gradient(width: int, height: int) -> Image.Image:
    """Fill the display with a gradient and add a simple white frame."""

    img = gradient_background(width, height)
    draw = ImageDraw.Draw(img)
    draw.rectangle((8, 8, width - 9, height - 9), outline=(255, 255, 255), width=4)
    return img


def draw_all(width: int, height: int, frame: int) -> Image.Image:
    """Combine gradient, square, and circle into one test frame."""

    img = gradient_background(width, height)
    draw = ImageDraw.Draw(img)
    margin = min(width, height) // 8
    draw.rectangle((margin, margin, width - margin, height - margin), fill=(0, 0, 0), outline=(255, 255, 255), width=4)
    radius = min(width, height) // 7
    x = width // 2 + int((frame % 20 - 10) * width / 80)
    y = height // 2
    draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=(0, 220, 255))
    return img


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vid", type=lambda s: int(s, 0), default=VID)
    parser.add_argument("--pid", type=lambda s: int(s, 0), default=PID)
    parser.add_argument("--mode", choices=("circle", "square", "gradient", "all"), default="all")
    parser.add_argument("--frames", type=int, default=1)
    parser.add_argument("--interval", type=float, default=0.5)
    parser.add_argument("--quality", type=int, default=60)
    parser.add_argument("--subsampling", type=int, default=2, choices=(0, 1, 2))
    parser.add_argument("--chunk-size", type=int, default=4096)
    args = parser.parse_args()

    display = ArtInChipDisplay(args.vid, args.pid, args.chunk_size)
    try:
        params = display.open()
        for frame in range(args.frames):
            if args.mode == "circle":
                img = draw_circle(params.width, params.height)
            elif args.mode == "square":
                img = draw_square(params.width, params.height)
            elif args.mode == "gradient":
                img = draw_gradient(params.width, params.height)
            else:
                img = draw_all(params.width, params.height, frame)
            display.send_image(img, frame, quality=args.quality, subsampling=args.subsampling)
            time.sleep(args.interval)
    except Exception:
        print_platform_hints()
        raise
    finally:
        display.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
