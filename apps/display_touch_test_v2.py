#!/usr/bin/env python3
"""Display/touch test v2: render logical DOWN/MOVE/UP events from aic_touch."""

from __future__ import annotations

import argparse
import io
import os
import time

from PIL import Image, ImageDraw, ImageFont

import aic_time
import aic_touch


def font(size: int, bold: bool = False):
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial Bold.ttf" if bold else "/Library/Fonts/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]
    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def text_width(draw: ImageDraw.ImageDraw, text: str, fnt) -> int:
    box = draw.textbbox((0, 0), text, font=fnt)
    return box[2] - box[0]


def jpeg_bytes(img: Image.Image, quality: int, subsampling: int) -> bytes:
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=quality, subsampling=subsampling, progressive=False)
    return out.getvalue()


def render_frame(
    width: int,
    height: int,
    x: int,
    y: int,
    active: bool,
    event_kind: str,
    reason: str,
    raw_x: int,
    raw_y: int,
    event_count: int,
    frame_id: int,
    touch_ok: bool,
) -> Image.Image:
    img = Image.new("RGB", (width, height), (8, 11, 17))
    draw = ImageDraw.Draw(img)

    for gx in range(0, width, 40):
        draw.line((gx, 0, gx, height), fill=(30, 39, 52))
    for gy in range(0, height, 40):
        draw.line((0, gy, width, gy), fill=(30, 39, 52))
    draw.rectangle((0, 0, width - 1, height - 1), outline=(70, 205, 245), width=3)

    color = (255, 66, 66) if active else (0, 216, 255)
    if event_kind == "up":
        color = (80, 235, 150)
    if not touch_ok:
        color = (255, 180, 40)

    arm = 58
    draw.line((x - arm, y, x + arm, y), fill=color, width=5)
    draw.line((x, y - arm, x, y + arm), fill=color, width=5)
    draw.ellipse((x - 20, y - 20, x + 20, y + 20), outline=(255, 255, 255), width=4)
    draw.ellipse((x - 5, y - 5, x + 5, y + 5), fill=color)

    big = font(24, bold=True)
    small = font(15)
    status = "NO TOUCH HID"
    if touch_ok:
        status = "TOUCH DOWN" if active else "TOUCH UP"
    draw.rounded_rectangle((14, 14, width - 14, 118), radius=8, fill=(0, 0, 0), outline=(72, 205, 245), width=1)
    draw.text((26, 24), status, fill=color, font=big)
    draw.text((26, 58), f"event={event_kind} reason={reason} count={event_count}", fill=(230, 237, 244), font=small)
    draw.text((26, 80), f"x={x:03d} y={y:03d} raw={raw_x:04d},{raw_y:04d} frame={frame_id}", fill=(230, 237, 244), font=small)

    hint = "tap, release, drag, release"
    hint_w = text_width(draw, hint, small)
    draw.text(((width - hint_w) // 2, height - 30), hint, fill=(160, 176, 190), font=small)
    return img


def run(args: argparse.Namespace) -> int:
    dev, ep_out, ep_in = aic_time.open_device(args.vid, args.pid)
    params = aic_time.get_params(dev)
    aic_time.authenticate(ep_out, ep_in, verbose=args.verbose, chunk_size=args.chunk_size)
    if args.verbose:
        print(params)

    x = params.width // 2
    y = params.height // 2
    raw_x = raw_y = aic_touch.RAW_MAX // 2
    active = False
    event_kind = "idle"
    reason = "start"
    event_count = 0
    touch_ok = True

    try:
        touch = aic_touch.TouchReader(
            params.width,
            params.height,
            args.vid,
            args.pid,
            tap_max_pixels=args.tap_max_pixels,
            stale_after_drag_reports=args.stale_after_drag_reports,
            stale_after_tap_reports=args.stale_after_tap_reports,
            still_pixels=args.stale_still_pixels,
            no_report_release_polls=args.no_report_release_polls,
        )
    except Exception as exc:
        touch = None
        touch_ok = False
        if args.verbose:
            print(f"touch unavailable: {exc}")

    frame_id = 0
    interval = 1.0 / max(1.0, args.fps)
    deadline = time.monotonic() + args.duration if args.duration > 0 else None
    next_frame = time.monotonic()
    event_since_frame = False
    try:
        while deadline is None or time.monotonic() < deadline:
            if touch is not None:
                while True:
                    event = touch.read(timeout_ms=1)
                    if event is None:
                        break
                    event_count += 1
                    event_kind = event.kind
                    reason = event.reason
                    event_since_frame = True
                    active = event.pressed
                    if event.kind in ("down", "move"):
                        x = event.x
                        y = event.y
                        raw_x = event.x_raw
                        raw_y = event.y_raw
                    if args.verbose:
                        print(
                            f"logical_event {event.kind:4s} active={int(event.pressed)} "
                            f"x={x} y={y} raw={raw_x},{raw_y} reason={event.reason} "
                            f"status=0x{event.status:02x} tip={int(event.tip_switch)} "
                            f"range={int(event.in_range)} contacts={event.contact_count}",
                            flush=True,
                        )

            now = time.monotonic()
            if now < next_frame:
                time.sleep(min(0.01, next_frame - now))
                continue

            shown_event_kind = event_kind if event_since_frame else ("hold" if active else "idle")
            shown_reason = reason if event_since_frame else "no-event"
            img = render_frame(
                params.width,
                params.height,
                x,
                y,
                active,
                shown_event_kind,
                shown_reason,
                raw_x,
                raw_y,
                event_count,
                frame_id,
                touch_ok,
            )
            jpeg = jpeg_bytes(img, args.quality, args.subsampling)
            aic_time.send_jpeg_frame(ep_out, jpeg, frame_id, args.chunk_size)
            if args.verbose:
                print(f"frame={frame_id} active={int(active)} event={shown_event_kind} reason={shown_reason} bytes={len(jpeg)}")
            frame_id += 1
            event_since_frame = False
            next_frame += interval
    except KeyboardInterrupt:
        if args.verbose:
            print("stopped")
    finally:
        if touch is not None:
            touch.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vid", type=lambda s: int(s, 0), default=aic_time.VID)
    parser.add_argument("--pid", type=lambda s: int(s, 0), default=aic_time.PID)
    parser.add_argument("--fps", type=float, default=10)
    parser.add_argument("--duration", type=float, default=0, help="seconds; 0 means forever")
    parser.add_argument("--quality", type=int, default=60)
    parser.add_argument("--subsampling", type=int, default=2, choices=(0, 1, 2))
    parser.add_argument("--chunk-size", type=int, default=4096)
    parser.add_argument("--tap-max-pixels", type=int, default=28)
    parser.add_argument("--stale-after-drag-reports", type=int, default=3)
    parser.add_argument("--stale-after-tap-reports", type=int, default=3)
    parser.add_argument("--stale-still-pixels", type=int, default=1)
    parser.add_argument("--no-report-release-polls", type=int, default=8)
    parser.add_argument("-v", "--verbose", action="store_true")
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
