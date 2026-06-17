"""Capture vkms panel frames (DRM framebuffer, with KWin fallback on Plasma)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Protocol

from PIL import Image

from msi_p13_display.drm_io import DrmOutput, capture_framebuffer


class CaptureBackend(Protocol):
    backend_name: str

    def close(self) -> None: ...

    def grab(self) -> Image.Image: ...


def _kde_screen_config() -> list[dict]:
    if not shutil.which("kscreen-doctor"):
        return []
    result = subprocess.run(
        ["kscreen-doctor", "-j"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, dict):
        return []
    outputs = payload.get("outputs")
    if not isinstance(outputs, list):
        return []
    return [output for output in outputs if output.get("enabled")]


def kde_output_rotation(screen_name: str) -> int:
    for output in _kde_screen_config():
        name = str(output.get("name") or "")
        if name == screen_name:
            return int(output.get("rotation", 1))
    return 1


def apply_kde_rotation(image: Image.Image, rotation: int) -> Image.Image:
    """Apply KScreen output rotation to a capture."""

    transforms: dict[int, tuple[Image.Transpose, ...]] = {
        1: (),
        2: (Image.Transpose.ROTATE_270,),
        4: (Image.Transpose.ROTATE_180,),
        8: (Image.Transpose.ROTATE_90,),
        16: (Image.Transpose.FLIP_LEFT_RIGHT,),
        32: (Image.Transpose.TRANSPOSE,),
        64: (Image.Transpose.FLIP_TOP_BOTTOM,),
        128: (Image.Transpose.TRANSVERSE,),
    }
    ops = transforms.get(rotation)
    if not ops:
        return image
    result = image
    for op in ops:
        result = result.transpose(op)
    return result


def _kwin_available() -> bool:
    try:
        import dbus  # noqa: F401
    except ImportError:
        return False
    if not shutil.which("gdbus"):
        return False
    result = subprocess.run(
        [
            "gdbus",
            "introspect",
            "--session",
            "--dest",
            "org.kde.KWin",
            "--object-path",
            "/org/kde/KWin/ScreenShot2",
        ],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    return result.returncode == 0


def _kwin_interface():
    import dbus
    from dbus.mainloop.glib import DBusGMainLoop

    DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()
    return dbus.Interface(
        bus.get_object("org.kde.KWin", "/org/kde/KWin/ScreenShot2"),
        "org.kde.KWin.ScreenShot2",
    )


def _kwin_capture_screen(screen_name: str) -> Image.Image:
    import dbus

    iface = _kwin_interface()
    options = {
        "include-cursor": dbus.Boolean(True),
        "native-resolution": dbus.Boolean(True),
        "include-decoration": dbus.Boolean(False),
        "include-shadow": dbus.Boolean(False),
    }
    read_fd, write_fd = os.pipe()
    try:
        results = iface.CaptureScreen(screen_name, options, dbus.types.UnixFd(write_fd))
    finally:
        os.close(write_fd)

    try:
        chunks = []
        while True:
            chunk = os.read(read_fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(read_fd)

    data = b"".join(chunks)
    if not data:
        raise RuntimeError("KWin CaptureScreen returned no image data")

    width = int(results["width"])
    height = int(results["height"])
    stride = int(results["stride"])
    with Image.frombytes("RGBA", (width, height), data, "raw", "BGRA", stride) as image:
        return image.convert("RGB")


class VkmsCapture:
    backend_name = "vkms"

    def __init__(self, drm_output: DrmOutput):
        self._drm = drm_output
        self._screen_name = drm_output.connector_name
        self._use_kwin = False
        self._warned = False

    def close(self) -> None:
        return None

    def __enter__(self) -> VkmsCapture:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _warn_kwin_fallback(self) -> None:
        if self._warned:
            return
        self._warned = True
        print(
            "warning: vkms DRM framebuffer is not readable under Plasma; "
            "using KWin ScreenShot2 for capture"
        )

    def grab(self) -> Image.Image:
        if self._use_kwin:
            return _kwin_capture_screen(self._screen_name)

        try:
            return capture_framebuffer(self._drm.card_path, self._drm.connector_id)
        except RuntimeError as exc:
            if not _kwin_available():
                raise RuntimeError(
                    "vkms DRM capture failed and KWin ScreenShot2 is unavailable. "
                    "On Fedora: sudo dnf install python3-dbus python3-gobject"
                ) from exc
            self._use_kwin = True
            self._warn_kwin_fallback()
            try:
                return _kwin_capture_screen(self._screen_name)
            except Exception as kwin_exc:
                if "NoAuthorized" in str(kwin_exc):
                    raise RuntimeError(
                        "KWin screen capture is not authorized for this process. "
                        "Install with ./scripts/install.sh and check "
                        "journalctl --user -u msi-p13-panel-monitor.service"
                    ) from kwin_exc
                raise RuntimeError("KWin ScreenShot2 capture failed.") from kwin_exc


def create_capture(drm_output: DrmOutput) -> CaptureBackend:
    """Create a capture backend for the vkms panel output."""

    return VkmsCapture(drm_output)
