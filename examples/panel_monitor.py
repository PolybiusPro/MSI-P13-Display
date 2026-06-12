#!/usr/bin/env python3
"""Expose the USB panel as its own monitor in the desktop compositor.

On KDE Plasma Wayland this registers a new output (for example ``Virtual-MSI-P13``)
in Display Settings. Drag windows onto that monitor; this script captures it
with the same APIs as other monitors and streams the frames to the USB panel.

Examples:

    python examples/panel_monitor.py
    python examples/panel_monitor.py --shell
    python examples/panel_monitor.py --name Panel --shell konsole
"""

from __future__ import annotations

import argparse
import sys
import time

from msi_p13_display.capture import kwin_capture_authorized, kwin_capture_setup_hint, resolve_capture_backend
from msi_p13_display.compositor_monitor import CompositorMonitor, default_shell
import usb.core

from msi_p13_display.display import (
    MsiP13Display,
    PID,
    UsbDeviceLostError,
    VID,
    is_device_gone_error,
    print_platform_hints,
)
from msi_p13_display.frame import parse_color
from msi_p13_display.stream import stream_frames


def log(message: str, *, quiet: bool) -> None:
    """Print status; in quiet mode write to stderr so startup logs capture it."""

    print(message, file=sys.stderr if quiet else sys.stdout, flush=True)


def open_usb_display(usb: MsiP13Display, *, retry_seconds: float, quiet: bool):
    """Open the USB panel, optionally waiting for the device to appear."""

    while True:
        try:
            return usb.open()
        except (RuntimeError, usb.core.USBError) as exc:
            if retry_seconds <= 0 or not is_device_gone_error(exc):
                raise
            log(f"waiting for USB display ({exc}); retrying in {retry_seconds:.0f}s", quiet=quiet)
            usb.close()
            time.sleep(retry_seconds)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Add a compositor monitor for the USB panel and stream it over USB.",
    )
    parser.add_argument(
        "--name",
        default="MSI-P13",
        help="monitor name in Display Settings (output becomes Virtual-NAME, default: MSI-P13)",
    )
    parser.add_argument(
        "--shell",
        nargs="?",
        const="auto",
        help="launch a terminal (default: konsole, foot, alacritty, ...)",
    )
    parser.add_argument(
        "--fps",
        type=float,
        default=None,
        help="target stream rate (default: device maximum, usually 60)",
    )
    parser.add_argument("--duration", type=float, default=0.0, help="seconds to run, 0 = until Ctrl+C")
    parser.add_argument(
        "--fit",
        choices=("cover", "contain", "stretch", "center"),
        default="stretch",
        help="panel mapping if capture size differs (default: stretch)",
    )
    parser.add_argument(
        "--background",
        type=parse_color,
        default=(8, 11, 18),
        help="letterbox color for contain/center modes",
    )
    parser.add_argument(
        "--capture-backend",
        default="auto",
        choices=("auto", "kwin", "spectacle"),
        help="desktop capture backend (default: auto, prefers kwin on KDE)",
    )
    parser.add_argument(
        "--no-pipeline",
        action="store_true",
        help="capture and send frames sequentially instead of pipelining",
    )
    parser.add_argument("--vid", type=lambda s: int(s, 0), default=VID)
    parser.add_argument("--pid", type=lambda s: int(s, 0), default=PID)
    parser.add_argument("--quality", type=int, default=60)
    parser.add_argument("--subsampling", type=int, default=2, choices=(0, 1, 2))
    parser.add_argument("--chunk-size", type=int, default=16384)
    parser.add_argument("--stats-every", type=int, default=60, help="print FPS every N frames (0 = off)")
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="suppress status output (recommended for the systemd service)",
    )
    parser.add_argument(
        "--retry-seconds",
        type=float,
        default=0.0,
        help="retry USB open when the device is missing (recommended for the systemd service)",
    )
    args = parser.parse_args()
    quiet = args.quiet
    if quiet and args.stats_every == 60:
        args.stats_every = 0

    usb = MsiP13Display(args.vid, args.pid, args.chunk_size)

    try:
        capture_backend = resolve_capture_backend(args.capture_backend)
        params = open_usb_display(usb, retry_seconds=args.retry_seconds, quiet=quiet)
        target_fps = args.fps if args.fps is not None else float(params.fps or 60)
        target_fps = min(target_fps, float(params.fps or 60))

        with CompositorMonitor(
            params.width,
            params.height,
            name=args.name,
            capture_backend=args.capture_backend,
        ) as monitor:
            output_name = monitor.output_name
            log(
                f"compositor monitor ready ({output_name}, {params.width}x{params.height})",
                quiet=quiet,
            )
            log("it appears in Display Settings; drag windows onto it.", quiet=quiet)
            log(f"capture backend: {capture_backend}", quiet=quiet)
            if capture_backend == "kwin" and not kwin_capture_authorized():
                log(f"note: {kwin_capture_setup_hint()}", quiet=quiet)

            if args.shell is not None:
                command = default_shell() if args.shell == "auto" else args.shell
                if not command:
                    log("warning: no terminal found; install konsole, foot, or alacritty", quiet=quiet)
                else:
                    monitor.launch(command, shell=True)
                    log(f"started shell: {command} (move it to {output_name} if needed)", quiet=quiet)

            while True:
                try:
                    log(f"streaming {output_name} to USB panel (target_fps={target_fps:.1f})", quiet=quiet)
                    stream_frames(
                        usb,
                        monitor,
                        width=params.width,
                        height=params.height,
                        fps=target_fps,
                        fit=args.fit,
                        background=args.background,
                        quality=args.quality,
                        subsampling=args.subsampling,
                        duration=args.duration,
                        stats_every=args.stats_every,
                        pipelined=not args.no_pipeline,
                    )
                    break
                except UsbDeviceLostError as exc:
                    if args.retry_seconds <= 0:
                        raise
                    log(f"USB display disconnected ({exc}); reconnecting...", quiet=quiet)
                    usb.close()
                    params = open_usb_display(usb, retry_seconds=args.retry_seconds, quiet=quiet)
    except KeyboardInterrupt:
        log("stopped", quiet=quiet)
    except Exception as exc:
        if quiet:
            print(f"panel monitor failed: {exc}", file=sys.stderr, flush=True)
        print_platform_hints(file=sys.stderr)
        raise
    finally:
        usb.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
