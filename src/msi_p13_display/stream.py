"""USB panel streaming loop with optional capture/send pipelining."""

from __future__ import annotations

import queue
import threading
import time
from typing import Protocol

import usb.core
from PIL import Image

from msi_p13_display.display import MsiP13Display, UsbDeviceLostError, is_device_gone_error
from msi_p13_display.frame import format_image


class FrameSource(Protocol):
    def grab(self) -> Image.Image: ...


def prepare_frame(
    image: Image.Image,
    width: int,
    height: int,
    fit: str,
    background: tuple[int, int, int],
) -> Image.Image:
    """Resize or crop *image* for the panel, skipping work when already sized."""

    rgb = image.convert("RGB")
    if fit == "stretch" and rgb.size == (width, height):
        return rgb
    return format_image(image, width, height, fit, background)


def stream_frames(
    usb: MsiP13Display,
    source: FrameSource,
    *,
    width: int,
    height: int,
    fps: float,
    fit: str,
    background: tuple[int, int, int],
    quality: int,
    subsampling: int,
    duration: float,
    stats_every: int,
    pipelined: bool = True,
) -> int:
    """Stream frames from *source* to *usb* at up to *fps* until duration elapses."""

    interval = 1.0 / max(0.1, fps)
    deadline = time.monotonic() + duration if duration > 0 else None
    frame_id = 0
    sent = 0
    started = time.monotonic()
    stop_event = threading.Event()
    capture_error: list[BaseException] = []
    frame_queue: queue.Queue[Image.Image] = queue.Queue(maxsize=1)

    def capture_worker() -> None:
        while not stop_event.is_set():
            try:
                frame = prepare_frame(source.grab(), width, height, fit, background)
            except BaseException as exc:
                capture_error.append(exc)
                stop_event.set()
                return
            try:
                frame_queue.put_nowait(frame)
            except queue.Full:
                try:
                    frame_queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    frame_queue.put_nowait(frame)
                except queue.Full:
                    pass

    if pipelined:
        threading.Thread(target=capture_worker, name="msi-p13-capture", daemon=True).start()

    try:
        while deadline is None or time.monotonic() < deadline:
            loop_start = time.monotonic()

            if pipelined:
                timeout = max(0.001, interval)
                try:
                    frame = frame_queue.get(timeout=timeout)
                except queue.Empty:
                    if capture_error:
                        raise capture_error[0]
                    continue
            else:
                frame = prepare_frame(source.grab(), width, height, fit, background)

            try:
                usb.send_image(frame, frame_id, quality, subsampling)
            except UsbDeviceLostError:
                raise
            except usb.core.USBError as exc:
                if is_device_gone_error(exc):
                    raise UsbDeviceLostError("USB display disconnected") from exc
                raise
            frame_id += 1
            sent += 1

            if stats_every > 0 and sent % stats_every == 0:
                elapsed = time.monotonic() - started
                print(f"sent {sent} frames, avg {sent / max(elapsed, 0.001):.1f} fps")

            spent = time.monotonic() - loop_start
            sleep_for = interval - spent
            if sleep_for > 0:
                time.sleep(sleep_for)
    finally:
        stop_event.set()

    if capture_error:
        raise capture_error[0]

    return sent
