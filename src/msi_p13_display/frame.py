"""Resize and crop source images for the fixed-size USB display."""

from __future__ import annotations

import argparse

from PIL import Image, ImageOps


def parse_color(value: str) -> tuple[int, int, int]:
    """Parse an RGB triplet like ``0,0,0`` or ``#101418``."""

    value = value.strip()
    if value.startswith("#"):
        if len(value) != 7:
            raise argparse.ArgumentTypeError("hex colors must look like #rrggbb")
        return (int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16))
    parts = value.split(",")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("RGB colors must look like 0,0,0")
    try:
        rgb = tuple(int(part.strip()) for part in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("RGB components must be integers") from exc
    if any(channel < 0 or channel > 255 for channel in rgb):
        raise argparse.ArgumentTypeError("RGB components must be in 0..255")
    return rgb


def format_image(
    image: Image.Image,
    width: int,
    height: int,
    fit: str,
    background: tuple[int, int, int],
) -> Image.Image:
    """Resize or crop *image* to the display dimensions."""

    rgb = image.convert("RGB")
    target = (width, height)

    if fit == "stretch":
        return rgb.resize(target, Image.Resampling.LANCZOS)

    if fit == "cover":
        return ImageOps.fit(rgb, target, method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))

    if fit == "contain":
        fitted = rgb.copy()
        fitted.thumbnail(target, Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", target, background)
        offset = ((width - fitted.width) // 2, (height - fitted.height) // 2)
        canvas.paste(fitted, offset)
        return canvas

    if fit == "center":
        canvas = Image.new("RGB", target, background)
        if rgb.width > width or rgb.height > height:
            left = max(0, (rgb.width - width) // 2)
            top = max(0, (rgb.height - height) // 2)
            rgb = rgb.crop((left, top, left + width, top + height))
        offset = ((width - rgb.width) // 2, (height - rgb.height) // 2)
        canvas.paste(rgb, offset)
        return canvas

    raise ValueError(f"unsupported fit mode: {fit}")
