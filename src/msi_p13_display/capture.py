"""Capture compositor monitor frames for panel_monitor on KDE Wayland."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Protocol

from PIL import Image

BACKENDS = ("auto", "kwin", "spectacle")


@dataclass(frozen=True)
class MonitorInfo:
    """One desktop monitor exposed by kscreen."""

    index: int
    left: int
    top: int
    width: int
    height: int
    name: str = ""


class CaptureBackend(Protocol):
    backend_name: str

    def close(self) -> None: ...

    def grab(self) -> Image.Image: ...


def is_wayland_session() -> bool:
    if os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
        return True
    return bool(os.environ.get("WAYLAND_DISPLAY"))


def tool_available(name: str) -> bool:
    return shutil.which(name) is not None


def resolve_backend(backend: str) -> str:
    choice = backend.lower()
    if choice == "auto":
        return resolve_capture_backend("auto")
    if choice not in BACKENDS:
        raise ValueError(f"unsupported backend {backend!r}; choose from {', '.join(BACKENDS)}")
    if choice == "kwin":
        if not _kwin_available():
            raise RuntimeError(
                "kwin backend requires KWin ScreenShot2 via system python3-dbus. "
                "On Fedora: sudo dnf install python3-dbus python3-gobject, then recreate "
                "the venv with python3 -m venv --system-site-packages .venv"
            )
        return "kwin"
    if choice == "spectacle" and not tool_available("spectacle"):
        raise RuntimeError("spectacle backend requires the spectacle executable in PATH")
    return choice


def resolve_capture_backend(backend: str) -> str:
    """Prefer KWin ScreenShot2 on KDE Wayland."""

    choice = backend.lower()
    if choice != "auto":
        return resolve_backend(choice)

    if is_wayland_session():
        desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
        session = os.environ.get("XDG_SESSION_DESKTOP", "").lower()
        if ("kde" in desktop or "plasma" in desktop or "kde" in session or "plasma" in session) and _kwin_available():
            return "kwin"
        if tool_available("spectacle"):
            return "spectacle"
    raise RuntimeError(
        "panel_monitor requires KDE Plasma Wayland with python3-dbus, or spectacle as a fallback"
    )


def _bounding_box(monitors: list[MonitorInfo]) -> MonitorInfo:
    physical = [monitor for monitor in monitors if monitor.index != 0]
    if not physical:
        return MonitorInfo(0, 0, 0, 0, 0, "all")
    left = min(monitor.left for monitor in physical)
    top = min(monitor.top for monitor in physical)
    right = max(monitor.left + monitor.width for monitor in physical)
    bottom = max(monitor.top + monitor.height for monitor in physical)
    return MonitorInfo(0, left, top, right - left, bottom - top, "all")


def _kde_output_size(output: dict) -> tuple[int, int]:
    current_mode = str(output.get("currentModeId", ""))
    for mode in output.get("modes", []):
        if str(mode.get("id")) == current_mode:
            size = mode.get("size") or {}
            return int(size.get("width", 0)), int(size.get("height", 0))
    size = output.get("size") or {}
    return int(size.get("width", 0)), int(size.get("height", 0))


def _kde_screen_config() -> list[dict]:
    if tool_available("kscreen-doctor"):
        result = subprocess.run(
            ["kscreen-doctor", "-j"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            try:
                payload = json.loads(result.stdout)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                outputs = [output for output in payload.get("outputs", []) if output.get("enabled")]
                if outputs:
                    return outputs

    for executable in ("qdbus6", "qdbus"):
        if not tool_available(executable):
            continue
        result = subprocess.run(
            [executable, "org.kde.KScreen", "/kscreen", "org.kde.KScreen.getConfig"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            continue
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            continue
        outputs = payload.get("outputs")
        if isinstance(outputs, list):
            return [output for output in outputs if output.get("enabled", True)]
    return []


def list_monitors(backend: str = "spectacle") -> list[MonitorInfo]:
    """Return enabled KDE outputs from kscreen."""

    resolve_backend(backend)
    outputs = _kde_screen_config()
    monitors: list[MonitorInfo] = []
    for index, output in enumerate(outputs, start=1):
        name = str(output.get("name") or output.get("id") or f"screen-{index}")
        pos = output.get("pos") or {}
        width, height = _kde_output_size(output)
        monitors.append(
            MonitorInfo(
                index,
                int(pos.get("x", 0)),
                int(pos.get("y", 0)),
                width,
                height,
                name,
            )
        )
    if monitors:
        monitors.insert(0, _bounding_box(monitors))
        return monitors
    return [MonitorInfo(0, 0, 0, 0, 0, "all"), MonitorInfo(1, 0, 0, 0, 0, "screen-1")]


def kwin_capture_authorized() -> bool:
    """Return whether this process may call KWin ScreenShot2."""

    if not _kwin_available():
        return False

    import dbus

    read_fd, write_fd = os.pipe()
    try:
        iface = _kwin_interface()
        iface.CaptureArea(0, 0, 1, 1, {}, dbus.types.UnixFd(write_fd))
    except Exception as exc:
        return "NoAuthorized" not in str(exc)
    finally:
        os.close(write_fd)
        try:
            os.close(read_fd)
        except OSError:
            pass
    return True


def kwin_capture_setup_hint() -> str:
    """Return setup instructions when KWin screenshot access is blocked."""

    return (
        "KWin screen capture may be blocked for this process. "
        "Install the systemd user service with ./scripts/install-panel-monitor.sh "
        "and check journalctl --user -u msi-p13-panel-monitor.service if capture fails."
    )


def _kwin_available() -> bool:
    try:
        import dbus  # noqa: F401
    except ImportError:
        return False
    if not tool_available("gdbus"):
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
    try:
        import dbus
        from dbus.mainloop.glib import DBusGMainLoop
    except ImportError as exc:
        raise RuntimeError(
            "KDE capture requires system D-Bus bindings. "
            "On Fedora: sudo dnf install python3-dbus python3-gobject, then recreate "
            "the venv with python3 -m venv --system-site-packages .venv"
        ) from exc

    DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()
    return dbus.Interface(
        bus.get_object("org.kde.KWin", "/org/kde/KWin/ScreenShot2"),
        "org.kde.KWin.ScreenShot2",
    )


def _kwin_capture_options(*, include_cursor: bool) -> dict:
    import dbus

    return {
        "include-cursor": dbus.Boolean(include_cursor),
        "native-resolution": dbus.Boolean(True),
        "include-decoration": dbus.Boolean(False),
        "include-shadow": dbus.Boolean(False),
    }


def _kwin_frame_from_pipe(iface, method: str, *args) -> Image.Image:
    import dbus

    read_fd, write_fd = os.pipe()
    try:
        results = getattr(iface, method)(*args, dbus.types.UnixFd(write_fd))
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
        raise RuntimeError(f"KWin {method} returned no image data")

    width = int(results["width"])
    height = int(results["height"])
    stride = int(results["stride"])
    with Image.frombytes("RGBA", (width, height), data, "raw", "BGRA", stride) as image:
        return image.convert("RGB")


def _kwin_capture(
    monitor: int,
    region: tuple[int, int, int, int] | None,
    *,
    screen_name: str | None = None,
    include_cursor: bool = False,
) -> Image.Image:
    iface = _kwin_interface()
    options = _kwin_capture_options(include_cursor=include_cursor)

    if screen_name:
        return _kwin_frame_from_pipe(iface, "CaptureScreen", screen_name, options)

    if region is not None:
        left, top, width, height = region
        return _kwin_frame_from_pipe(iface, "CaptureArea", left, top, width, height, options)

    monitors = list_monitors()
    if monitor < 0 or monitor >= len(monitors):
        available = ", ".join(str(item.index) for item in monitors)
        raise ValueError(f"monitor index {monitor} is out of range; available: {available}")

    target = monitors[monitor]
    if target.name and target.name not in {"all", "screen-1"}:
        return _kwin_frame_from_pipe(iface, "CaptureScreen", target.name, options)

    return _kwin_frame_from_pipe(iface, "CaptureActiveScreen", options)


def kde_output_rotation(screen_name: str) -> int:
    for output in _kde_screen_config():
        name = str(output.get("name") or "")
        if name == screen_name:
            return int(output.get("rotation", 1))
    return 1


def apply_kde_rotation(image: Image.Image, rotation: int) -> Image.Image:
    """Apply KScreen output rotation to a capture.

    KWin ScreenShot2 returns an unrotated buffer for virtual outputs, so the
    compositor rotation from kscreen must be applied in software.
    """

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


def _spectacle_cli_capture(monitor: int, path: str, *, include_pointer: bool = False) -> None:
    command = ["spectacle", "--background", "--nonotify", "--output", path]
    if include_pointer:
        command.append("--pointer")
    if monitor == 0:
        command.append("--fullscreen")
    elif monitor == 1:
        command.append("--current")
    else:
        command.append("--fullscreen")
    result = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"spectacle capture failed ({result.returncode}): {stderr}")


def _spectacle_grab(monitor: int, *, include_pointer: bool = False) -> Image.Image:
    monitors = list_monitors()
    if monitor < 0 or monitor >= len(monitors):
        available = ", ".join(str(item.index) for item in monitors)
        raise ValueError(f"monitor index {monitor} is out of range; available: {available}")

    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        _spectacle_cli_capture(1 if monitor == 1 else 0, path, include_pointer=include_pointer)
        with Image.open(path) as image:
            rgb = image.convert("RGB")
            if monitor <= 1:
                return rgb
            target = monitors[monitor]
            if target.width <= 0 or target.height <= 0:
                raise RuntimeError(f"unknown geometry for monitor {monitor} ({target.name})")
            box = (
                target.left,
                target.top,
                target.left + target.width,
                target.top + target.height,
            )
            cropped = rgb.crop(box)
            if target.name:
                return apply_kde_rotation(cropped, kde_output_rotation(target.name))
            return cropped
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


class KwinCapture:
    backend_name = "kwin"

    def __init__(
        self,
        monitor: int = 1,
        region: tuple[int, int, int, int] | None = None,
        *,
        screen_name: str | None = None,
        include_cursor: bool = False,
    ):
        self._monitor = monitor
        self._region = region
        self._screen_name = screen_name
        self._include_cursor = include_cursor
        self._using_fallback = False
        self._warned = False

    def close(self) -> None:
        return None

    def __enter__(self) -> KwinCapture:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _warn_fallback(self, reason: str) -> None:
        if self._warned:
            return
        self._warned = True
        print(f"warning: {reason}")
        print(f"  {kwin_capture_setup_hint()}")

    def _spectacle_fallback(self) -> Image.Image:
        return _spectacle_grab(self._monitor, include_pointer=self._include_cursor)

    def grab(self) -> Image.Image:
        if self._using_fallback:
            return self._spectacle_fallback()

        if not _kwin_available():
            self._using_fallback = True
            self._warn_fallback("KWin ScreenShot2 is unavailable.")
            return self._spectacle_fallback()

        try:
            return _kwin_capture(
                self._monitor,
                self._region,
                screen_name=self._screen_name,
                include_cursor=self._include_cursor,
            )
        except Exception as exc:
            if "NoAuthorized" in str(exc):
                self._using_fallback = True
                self._warn_fallback("KWin screen capture is not authorized for this process.")
                return self._spectacle_fallback()
            raise RuntimeError("KWin ScreenShot2 capture failed.") from exc


class SpectacleCapture:
    backend_name = "spectacle"

    def __init__(
        self,
        monitor: int = 1,
        *,
        screen_name: str | None = None,
        include_cursor: bool = False,
    ):
        self._monitor = monitor
        self._screen_name = screen_name
        self._include_cursor = include_cursor

    def close(self) -> None:
        return None

    def __enter__(self) -> SpectacleCapture:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def grab(self) -> Image.Image:
        if _kwin_available() and self._screen_name:
            try:
                return _kwin_capture(
                    self._monitor,
                    None,
                    screen_name=self._screen_name,
                    include_cursor=self._include_cursor,
                )
            except Exception as exc:
                if "NoAuthorized" not in str(exc):
                    raise
        return _spectacle_grab(self._monitor, include_pointer=self._include_cursor)


def create_capture(
    monitor: int = 1,
    region: tuple[int, int, int, int] | None = None,
    backend: str = "auto",
    *,
    screen_name: str | None = None,
    include_cursor: bool = False,
) -> CaptureBackend:
    """Create a capture backend for the panel monitor."""

    resolved = resolve_backend(backend)
    if resolved == "spectacle":
        return SpectacleCapture(monitor, screen_name=screen_name, include_cursor=include_cursor)
    return KwinCapture(
        monitor,
        region,
        screen_name=screen_name,
        include_cursor=include_cursor,
    )
