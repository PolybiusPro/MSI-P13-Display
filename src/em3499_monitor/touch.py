#!/usr/bin/env python3
"""Read logical touch events from the ArtInChip HID digitizer.

The display and the touch panel are separate USB interfaces. The framebuffer is
sent over vendor bulk interface 0. Touch reports are read from HID interface 3.
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass, replace

import hid


VID = 0x33C3
PID = 0x0E02
HID_INTERFACE = 3
RAW_MAX = 4095


@dataclass(frozen=True)
class TouchEvent:
    """One parsed and optionally filtered touch event."""

    kind: str
    pressed: bool
    x_raw: int
    y_raw: int
    x: int
    y: int
    status: int
    tip_switch: bool
    in_range: bool
    contact_count: int
    reason: str
    report: bytes


def find_touch_path(vid: int = VID, pid: int = PID) -> bytes:
    """Find the hidapi path for interface 3."""

    for item in hid.enumerate(vid, pid):
        if item.get("interface_number") == HID_INTERFACE:
            return item["path"]
    raise RuntimeError(f"touch HID interface not found for {vid:04x}:{pid:04x}")


def parse_report(report: bytes, width: int, height: int) -> TouchEvent | None:
    """Parse the observed 0x01 or 0x54 HID report formats."""

    if len(report) < 8:
        return None
    if report[0] == 0x01:
        offset = 3
        status = report[1]
    elif report[0] == 0x54:
        offset = 4
        status = report[2] if len(report) > 2 else 0
    else:
        return None

    x_raw = report[offset] | (report[offset + 1] << 8)
    y_raw = report[offset + 2] | (report[offset + 3] << 8)
    contact_count = report[-1]
    tip_switch = bool(status & 0x01)
    in_range = bool(status & 0x02)
    pressed = tip_switch and contact_count > 0
    x = max(0, min(width - 1, round(x_raw * (width - 1) / RAW_MAX)))
    y = max(0, min(height - 1, round(y_raw * (height - 1) / RAW_MAX)))
    return TouchEvent("raw", pressed, x_raw, y_raw, x, y, status, tip_switch, in_range, contact_count, "raw", report)


class TouchStateFilter:
    """Convert noisy capacitive reports into down/move/up events.

    During testing this controller sometimes kept sending repeated pressed
    reports after release, especially after a drag. The filter treats repeated
    still coordinates and repeated no-report polls as release evidence.
    """

    def __init__(self, tap_max_pixels: int = 28, stale_reports: int = 3, still_pixels: int = 1, gap_reports: int = 8):
        self.tap_max_pixels = tap_max_pixels
        self.stale_reports_limit = max(1, stale_reports)
        self.still_pixels = max(0, still_pixels)
        self.gap_reports_limit = max(1, gap_reports)
        self.logical_down = False
        self.down_x = self.down_y = 0
        self.last_x = self.last_y = 0
        self.last_event: TouchEvent | None = None
        self.stale_count = 0
        self.gap_count = 0
        self.suppress_stale = False
        self.suppress_x = 0
        self.suppress_y = 0

    def distance(self, event: TouchEvent, x: int, y: int) -> int:
        return max(abs(event.x - x), abs(event.y - y))

    def finish(self, event: TouchEvent, reason: str) -> TouchEvent:
        last = self.last_event or event
        self.logical_down = False
        self.last_event = None
        self.stale_count = 0
        self.gap_count = 0
        return replace(last, kind="up", pressed=False, tip_switch=False, in_range=False, contact_count=0, reason=reason)

    def no_report(self) -> TouchEvent | None:
        if not self.logical_down or self.last_event is None:
            return None
        self.gap_count += 1
        if self.gap_count >= self.gap_reports_limit:
            return self.finish(self.last_event, "gap")
        return None

    def process(self, event: TouchEvent) -> TouchEvent | None:
        self.gap_count = 0
        if not event.pressed:
            self.suppress_stale = False
            return self.finish(event, "up") if self.logical_down else None
        if self.suppress_stale:
            if self.distance(event, self.suppress_x, self.suppress_y) <= self.still_pixels:
                return None
            self.suppress_stale = False
            self.logical_down = True
            self.down_x = self.last_x = event.x
            self.down_y = self.last_y = event.y
            self.last_event = event
            return replace(event, kind="down", reason="down")
        if not self.logical_down:
            self.logical_down = True
            self.down_x = self.last_x = event.x
            self.down_y = self.last_y = event.y
            self.last_event = event
            return replace(event, kind="down", reason="down")

        if self.distance(event, self.last_x, self.last_y) <= self.still_pixels:
            self.stale_count += 1
            if self.stale_count >= self.stale_reports_limit:
                self.suppress_stale = True
                self.suppress_x = event.x
                self.suppress_y = event.y
                return self.finish(event, "stale")
            return None

        self.stale_count = 0
        self.last_x = event.x
        self.last_y = event.y
        self.last_event = event
        return replace(event, kind="move", reason="move")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--width", type=int, default=480)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--raw", action="store_true")
    parser.add_argument("--duration", type=float, default=0)
    args = parser.parse_args()

    dev = hid.device()
    dev.open_path(find_touch_path())
    dev.set_nonblocking(True)
    filt = TouchStateFilter()
    deadline = time.monotonic() + args.duration if args.duration > 0 else None
    try:
        while deadline is None or time.monotonic() < deadline:
            data = dev.read(64, timeout_ms=25)
            if data:
                event = parse_report(bytes(data), args.width, args.height)
                if event is not None and not args.raw:
                    event = filt.process(event)
            else:
                event = None if args.raw else filt.no_report()
            if event is None:
                time.sleep(0.01)
                continue
            print(
                f"{time.strftime('%H:%M:%S')} {event.kind:4s} pressed={int(event.pressed)} "
                f"x={event.x:3d} y={event.y:3d} raw={event.x_raw:4d},{event.y_raw:4d} "
                f"status=0x{event.status:02x} contacts={event.contact_count} reason={event.reason}",
                flush=True,
            )
    finally:
        dev.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
