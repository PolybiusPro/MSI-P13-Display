#!/usr/bin/env python3
"""Load an image file, format it for the display, and send it over USB.

Supports still images (JPEG, PNG, WebP, ...) and animated GIF / WebP files.

Examples:

    python examples/send_image.py photo.jpg
    python examples/send_image.py wallpaper.png --fit contain --background 0,0,0
    python examples/send_image.py animation.gif
    python examples/send_image.py animation.webp --anim-loops 0 --anim-speed 1.5
    python examples/send_image.py album/*.jpg --interval 5
"""

from __future__ import annotations

import argparse
import time
from collections.abc import Iterator
from pathlib import Path

from PIL import Image, ImageSequence

from msi_p13_display.display import MsiP13Display, PID, VID, print_platform_hints
from msi_p13_display.frame import format_image, parse_color

ANIMATED_SUFFIXES = {".gif", ".webp"}
ANIMATED_FORMATS = {"GIF", "WEBP"}


def is_animated_path(path: Path) -> bool:
    """Return True for file types that may contain animation."""

    return path.suffix.lower() in ANIMATED_SUFFIXES


def frame_duration_ms(
    frame: Image.Image,
    image: Image.Image,
    default_ms: int,
    index: int = 0,
) -> int:
    """Read a frame delay in milliseconds when Pillow exposes it."""

    for candidate in (frame.info.get("duration"), image.info.get("duration")):
        if candidate is None:
            continue
        if isinstance(candidate, (list, tuple)):
            if index < len(candidate):
                return max(1, int(candidate[index]))
            return max(1, int(candidate[0]))
        return max(1, int(candidate))
    return default_ms


def resolve_play_loops(image: Image.Image, anim_loops: int) -> int | None:
    """Return loop count for an animation, or None for infinite playback."""

    if anim_loops == 0:
        return None
    if anim_loops < 0:
        file_loop = int(image.info.get("loop", 0))
        return None if file_loop == 0 else file_loop
    return anim_loops


def iter_gif_frames(image: Image.Image) -> Iterator[tuple[Image.Image, float]]:
    """Yield composited RGBA GIF frames and per-frame delay in seconds."""

    default_ms = max(1, int(image.info.get("duration") or 100))
    if not getattr(image, "is_animated", False):
        yield image, default_ms / 1000.0
        return

    canvas = Image.new("RGBA", image.size)
    for index, frame in enumerate(ImageSequence.Iterator(image)):
        duration_ms = frame_duration_ms(frame, image, default_ms, index)
        patch = frame.convert("RGBA")
        canvas.paste(patch, (0, 0), patch)
        yield canvas.copy(), duration_ms / 1000.0


def iter_webp_frames(image: Image.Image) -> Iterator[tuple[Image.Image, float]]:
    """Yield RGBA WebP frames and per-frame delay in seconds."""

    default_ms = max(1, int(image.info.get("duration") or 100))
    if not getattr(image, "is_animated", False):
        yield image, default_ms / 1000.0
        return

    for index, frame in enumerate(ImageSequence.Iterator(image)):
        duration_ms = frame_duration_ms(frame, image, default_ms, index)
        yield frame.convert("RGBA"), duration_ms / 1000.0


def iter_animated_frames(image: Image.Image) -> Iterator[tuple[Image.Image, float]]:
    """Yield frames for a supported animated image format."""

    if image.format == "GIF":
        yield from iter_gif_frames(image)
        return
    if image.format == "WEBP":
        yield from iter_webp_frames(image)
        return
    raise ValueError(f"unsupported animated format: {image.format}")


def send_still(
    display: MsiP13Display,
    path: Path,
    width: int,
    height: int,
    fit: str,
    background: tuple[int, int, int],
    quality: int,
    subsampling: int,
    frame_id: int,
) -> int:
    """Send one still image and return the next frame id."""

    with Image.open(path) as source:
        frame = format_image(source, width, height, fit, background)
    display.send_image(frame, frame_id, quality, subsampling)
    print(f"sent {path} ({width}x{height}, fit={fit})")
    return frame_id + 1


def send_animated(
    display: MsiP13Display,
    path: Path,
    width: int,
    height: int,
    fit: str,
    background: tuple[int, int, int],
    quality: int,
    subsampling: int,
    frame_id: int,
    *,
    anim_loops: int,
    anim_speed: float,
    min_interval: float,
) -> int:
    """Play an animated GIF or WebP file and return the next frame id."""

    with Image.open(path) as source:
        if source.format not in ANIMATED_FORMATS:
            raise ValueError(f"unsupported animated file: {path}")

        if not getattr(source, "is_animated", False):
            frame = format_image(source, width, height, fit, background)
            display.send_image(frame, frame_id, quality, subsampling)
            print(f"sent {path} ({width}x{height}, fit={fit})")
            return frame_id + 1

        frame_count = getattr(source, "n_frames", 1)
        play_loops = resolve_play_loops(source, anim_loops)
        loop_num = 0
        while play_loops is None or loop_num < play_loops:
            for patch, delay in iter_animated_frames(source):
                frame = format_image(patch, width, height, fit, background)
                display.send_image(frame, frame_id, quality, subsampling)
                frame_id += 1
                sleep_for = max(min_interval, delay / max(0.1, anim_speed))
                time.sleep(sleep_for)

            loop_num += 1
            label = f"loop {loop_num}" if play_loops is None else f"loop {loop_num}/{play_loops}"
            print(f"sent {path} ({source.format.lower()}, {frame_count} frames, {label}, fit={fit})")

        return frame_id


def load_paths(paths: list[str]) -> list[Path]:
    """Expand CLI paths and fail early when nothing was found."""

    files: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if not path.is_file():
            raise FileNotFoundError(f"image not found: {path}")
        files.append(path)
    if not files:
        raise SystemExit("provide at least one image path")
    return files


def add_anim_args(parser: argparse.ArgumentParser) -> None:
    """Register animation playback options."""

    parser.add_argument("--interval", type=float, default=0.0, help="seconds between still images in a slideshow")
    parser.add_argument("--repeat", type=int, default=1, help="send the file list this many times")
    parser.add_argument(
        "--anim-loops",
        "--gif-loops",
        type=int,
        default=1,
        dest="anim_loops",
        help="animation loops: 0 = infinite, 1 = once, -1 = use loop metadata (0 in file means infinite)",
    )
    parser.add_argument(
        "--anim-speed",
        "--gif-speed",
        type=float,
        default=1.0,
        dest="anim_speed",
        help="animation playback speed multiplier",
    )
    parser.add_argument(
        "--min-interval",
        type=float,
        default=0.05,
        help="minimum seconds between animation frames (default: 0.05)",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Send one or more image files to the USB display.")
    parser.add_argument("images", nargs="+", help="image, GIF, or animated WebP file paths")
    parser.add_argument("--vid", type=lambda s: int(s, 0), default=VID)
    parser.add_argument("--pid", type=lambda s: int(s, 0), default=PID)
    parser.add_argument(
        "--fit",
        choices=("cover", "contain", "stretch", "center"),
        default="cover",
        help="how to map the source image onto the display (default: cover)",
    )
    parser.add_argument(
        "--background",
        type=parse_color,
        default=(8, 11, 18),
        help="letterbox color for contain/center modes (default: 8,11,18)",
    )
    add_anim_args(parser)
    parser.add_argument("--quality", type=int, default=60)
    parser.add_argument("--subsampling", type=int, default=2, choices=(0, 1, 2))
    parser.add_argument("--chunk-size", type=int, default=4096)
    args = parser.parse_args()

    image_paths = load_paths(args.images)
    display = MsiP13Display(args.vid, args.pid, args.chunk_size)
    frame_id = 0

    try:
        params = display.open()
        for _ in range(max(1, args.repeat)):
            for path in image_paths:
                if is_animated_path(path):
                    frame_id = send_animated(
                        display,
                        path,
                        params.width,
                        params.height,
                        args.fit,
                        args.background,
                        args.quality,
                        args.subsampling,
                        frame_id,
                        anim_loops=args.anim_loops,
                        anim_speed=args.anim_speed,
                        min_interval=args.min_interval,
                    )
                else:
                    frame_id = send_still(
                        display,
                        path,
                        params.width,
                        params.height,
                        args.fit,
                        args.background,
                        args.quality,
                        args.subsampling,
                        frame_id,
                    )
                if args.interval > 0:
                    time.sleep(args.interval)
    except Exception:
        print_platform_hints()
        raise
    finally:
        display.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
