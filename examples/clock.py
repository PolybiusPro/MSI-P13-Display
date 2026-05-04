#!/usr/bin/env python3
"""Minimal clock example for the ArtInChip USB display."""

from __future__ import annotations

import argparse
import os
import time

from PIL import Image, ImageDraw, ImageFont

from em3499_monitor.display import ArtInChipDisplay, PID, VID, print_platform_hints


def load_font(size: int):
    """Pick a common macOS/Linux font, falling back to Pillow's default."""

    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def make_clock(width: int, height: int) -> Image.Image:
    """Render one clock frame."""

    img = Image.new("RGB", (width, height), (4, 6, 10))
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 0, width - 1, height - 1), outline=(0, 170, 255), width=8)

    text = time.strftime("%H:%M:%S")
    font = load_font(max(24, min(width, height) // 5))
    box = draw.textbbox((0, 0), text, font=font)
    draw.text(((width - (box[2] - box[0])) // 2, (height - (box[3] - box[1])) // 2), text, fill=(255, 255, 255), font=font)
    return img


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vid", type=lambda s: int(s, 0), default=VID)
    parser.add_argument("--pid", type=lambda s: int(s, 0), default=PID)
    parser.add_argument("--duration", type=float, default=10)
    parser.add_argument("--fps", type=float, default=1)
    parser.add_argument("--quality", type=int, default=60)
    parser.add_argument("--subsampling", type=int, default=2, choices=(0, 1, 2))
    parser.add_argument("--chunk-size", type=int, default=4096)
    args = parser.parse_args()

    display = ArtInChipDisplay(args.vid, args.pid, args.chunk_size)
    try:
        params = display.open()
        interval = 1.0 / max(0.1, args.fps)
        deadline = time.monotonic() + args.duration if args.duration > 0 else None
        frame = 0
        while deadline is None or time.monotonic() < deadline:
            display.send_image(make_clock(params.width, params.height), frame, args.quality, args.subsampling)
            frame += 1
            time.sleep(interval)
    except Exception:
        print_platform_hints()
        raise
    finally:
        display.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
