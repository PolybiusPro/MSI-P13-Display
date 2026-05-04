#!/usr/bin/env python3
"""Read touch events from the ArtInChip eM3499 HID digitizer."""

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
    kind: str
    pressed: bool
    x_raw: int
    y_raw: int
    x: int
    y: int
    report: bytes
    status: int = 0
    tip_switch: bool = False
    in_range: bool = False
    contact_count: int = 0
    contact_id: int = 0
    reason: str = "raw"


def find_touch_path(vid: int = VID, pid: int = PID) -> bytes:
    for item in hid.enumerate(vid, pid):
        if item.get("interface_number") == HID_INTERFACE:
            return item["path"]
    raise RuntimeError(f"touch HID interface not found for {vid:04x}:{pid:04x}")


def parse_report(report: bytes, width: int, height: int) -> TouchEvent | None:
    if len(report) < 8:
        return None
    if report[0] == 0x01:
        offset = 3
        status = report[1]
        contact_id = report[2]
    elif report[0] == 0x54:
        offset = 4
        status = report[2] if len(report) > 2 else 0
        contact_id = report[3] if len(report) > 3 else 0
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
    return TouchEvent(
        "raw",
        pressed,
        x_raw,
        y_raw,
        x,
        y,
        report,
        status=status,
        tip_switch=tip_switch,
        in_range=in_range,
        contact_count=contact_count,
        contact_id=contact_id,
    )


class TouchReader:
    def __init__(
        self,
        width: int = 480,
        height: int = 480,
        vid: int = VID,
        pid: int = PID,
        filter_stale: bool = True,
        tap_max_pixels: int = 28,
        stale_after_drag_reports: int = 3,
        stale_after_tap_reports: int = 3,
        still_pixels: int = 1,
        no_report_release_polls: int = 8,
    ):
        self.width = width
        self.height = height
        self.device = hid.device()
        self.device.open_path(find_touch_path(vid, pid))
        self.device.set_nonblocking(True)
        self.filter = (
            TouchStateFilter(
                tap_max_pixels,
                stale_after_drag_reports,
                stale_after_tap_reports,
                still_pixels,
                no_report_release_polls,
            )
            if filter_stale
            else None
        )

    def read(self, timeout_ms: int = 25) -> TouchEvent | None:
        data = self.device.read(64, timeout_ms=timeout_ms)
        if not data:
            if self.filter is None:
                return None
            return self.filter.process_no_report()
        event = parse_report(bytes(data), self.width, self.height)
        if event is None or self.filter is None:
            return event
        return self.filter.process(event)

    def close(self):
        self.device.close()


class TouchStateFilter:
    """Turn raw capacitive reports into stable logical touch events.

    This controller can keep sending a stale Tip Switch report after a drag, or
    stop reporting without a clean inactive report. Repeated no-motion reports
    and repeated read gaps are treated as logical release signals.
    """

    def __init__(
        self,
        tap_max_pixels: int,
        stale_after_drag_reports: int,
        stale_after_tap_reports: int,
        still_pixels: int,
        no_report_release_polls: int,
    ):
        self.tap_max_pixels = tap_max_pixels
        self.stale_after_drag_reports = max(1, stale_after_drag_reports)
        self.stale_after_tap_reports = max(1, stale_after_tap_reports)
        self.still_pixels = max(0, still_pixels)
        self.no_report_release_polls = max(1, no_report_release_polls)
        self.logical_down = False
        self.drag_started = False
        self.down_x = 0
        self.down_y = 0
        self.last_x = 0
        self.last_y = 0
        self.last_event: TouchEvent | None = None
        self.stale_reports = 0
        self.gap_reports = 0
        self.suppress_stale = False
        self.suppress_x = 0
        self.suppress_y = 0

    def distance(self, event: TouchEvent, x: int, y: int) -> int:
        return max(abs(event.x - x), abs(event.y - y))

    def start_contact(self, event: TouchEvent) -> TouchEvent:
        self.logical_down = True
        self.drag_started = False
        self.down_x = self.last_x = event.x
        self.down_y = self.last_y = event.y
        self.last_event = event
        self.stale_reports = 0
        self.gap_reports = 0
        return replace(event, kind="down", reason="down")

    def finish_contact(self, event: TouchEvent, reason: str) -> TouchEvent:
        last = self.last_event or event
        finished = replace(
            last,
            kind="up",
            pressed=False,
            tip_switch=False,
            in_range=False,
            contact_count=0,
            reason=reason,
        )
        self.logical_down = False
        self.drag_started = False
        self.last_event = None
        self.stale_reports = 0
        self.gap_reports = 0
        return finished

    def process_no_report(self) -> TouchEvent | None:
        if not self.logical_down or self.last_event is None:
            return None
        self.gap_reports += 1
        if self.gap_reports >= self.no_report_release_polls:
            return self.finish_contact(self.last_event, "gap")
        return None

    def process(self, event: TouchEvent) -> TouchEvent | None:
        self.gap_reports = 0
        if not event.pressed:
            self.suppress_stale = False
            if self.logical_down:
                return self.finish_contact(event, "up")
            return None

        if self.suppress_stale:
            if self.distance(event, self.suppress_x, self.suppress_y) <= self.still_pixels:
                return None
            self.suppress_stale = False
            return self.start_contact(event)

        if not self.logical_down:
            return self.start_contact(event)

        moved_total = self.distance(event, self.down_x, self.down_y)
        moved_step = self.distance(event, self.last_x, self.last_y)
        if moved_total > self.tap_max_pixels:
            self.drag_started = True

        stale_limit = self.stale_after_drag_reports if self.drag_started else self.stale_after_tap_reports
        if moved_step <= self.still_pixels:
            self.stale_reports += 1
            if self.stale_reports >= stale_limit:
                self.suppress_stale = True
                self.suppress_x = event.x
                self.suppress_y = event.y
                return self.finish_contact(event, "stale")
            return None
        else:
            self.stale_reports = 0

        self.last_x = event.x
        self.last_y = event.y
        self.last_event = event
        return replace(event, kind="move", reason="move")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--width", type=int, default=480)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--duration", type=float, default=0, help="seconds; 0 means forever")
    parser.add_argument("--raw", action="store_true")
    parser.add_argument("--raw-events", action="store_true", help="disable stale-contact state filtering")
    parser.add_argument("--stale-after-tap-reports", type=int, default=3)
    parser.add_argument("--stale-after-drag-reports", type=int, default=3)
    parser.add_argument("--stale-still-pixels", type=int, default=1)
    parser.add_argument("--no-report-release-polls", type=int, default=8)
    args = parser.parse_args()

    reader = TouchReader(
        args.width,
        args.height,
        filter_stale=not args.raw_events,
        stale_after_tap_reports=args.stale_after_tap_reports,
        stale_after_drag_reports=args.stale_after_drag_reports,
        still_pixels=args.stale_still_pixels,
        no_report_release_polls=args.no_report_release_polls,
    )
    deadline = time.monotonic() + args.duration if args.duration > 0 else None
    last: TouchEvent | None = None
    last_down: TouchEvent | None = None
    try:
        while deadline is None or time.monotonic() < deadline:
            event = reader.read()
            if event is None:
                time.sleep(0.01)
                continue
            if last and event.pressed == last.pressed and event.x == last.x and event.y == last.y:
                continue
            state = event.kind if event.kind != "raw" else ("down" if event.pressed else "up")
            shown = event if event.pressed or last_down is None else last_down
            line = (
                f"{time.strftime('%H:%M:%S')} {state:4s} x={shown.x:3d} y={shown.y:3d} "
                f"raw={shown.x_raw:4d},{shown.y_raw:4d} status=0x{event.status:02x} "
                f"tip={int(event.tip_switch)} range={int(event.in_range)} "
                f"contacts={event.contact_count} reason={event.reason}"
            )
            if args.raw:
                line += " " + event.report.hex(" ")
            print(line, flush=True)
            if event.pressed:
                last_down = event
            last = event
    finally:
        reader.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
