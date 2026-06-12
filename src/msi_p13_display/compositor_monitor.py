"""Register a compositor output that appears as its own monitor and stream it to the USB panel."""

from __future__ import annotations

import os
import random
import signal
import socket
import subprocess
import time
from dataclasses import dataclass, field

from PIL import Image

from msi_p13_display.capture import (
    CaptureBackend,
    MonitorInfo,
    apply_kde_rotation,
    create_capture,
    is_wayland_session,
    kde_output_rotation,
    list_monitors,
    resolve_capture_backend,
    tool_available,
)


def default_shell() -> str | None:
    for candidate in ("konsole", "foot", "alacritty", "kitty", "wezterm", "ghostty", "xterm"):
        if tool_available(candidate):
            return candidate
    return None


def compositor_output_name(monitor_name: str) -> str:
    return f"Virtual-{monitor_name}"


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class CompositorMonitor:
    """A KDE/KWin output that shows up as its own monitor in Display Settings."""

    width: int
    height: int
    name: str = "MSI-P13"
    capture_backend: str = "auto"
    _output_name: str | None = field(default=None, init=False, repr=False)
    _monitor: MonitorInfo | None = field(default=None, init=False, repr=False)
    _service_proc: subprocess.Popen | None = field(default=None, init=False, repr=False)
    _children: list[subprocess.Popen] = field(default_factory=list, init=False, repr=False)
    _capture: CaptureBackend | None = field(default=None, init=False, repr=False)
    _last_rotation: int | None = field(default=None, init=False, repr=False)

    @property
    def output_name(self) -> str:
        if self._output_name is None:
            raise RuntimeError("compositor monitor is not started")
        return self._output_name

    @property
    def monitor_index(self) -> int:
        if self._monitor is None:
            raise RuntimeError("compositor monitor is not started")
        return self._monitor.index

    def start(self) -> str:
        """Add a compositor monitor and return its output name."""

        if self._output_name is not None:
            return self._output_name

        if not is_wayland_session():
            raise RuntimeError("panel_monitor requires a Wayland session.")
        if not tool_available("krfb-virtualmonitor"):
            raise RuntimeError(
                "KDE compositor monitors require krfb-virtualmonitor "
                "(Fedora: sudo dnf install krfb)"
            )

        port = _find_free_port()
        password = f"msi-p13-{random.randint(0, 999_999):06d}"
        command = [
            "krfb-virtualmonitor",
            "--resolution",
            f"{self.width}x{self.height}",
            "--name",
            self.name,
            "--password",
            password,
            "--port",
            str(port),
        ]
        self._service_proc = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            self._monitor = self._wait_for_output(compositor_output_name(self.name))
        except Exception:
            self.stop()
            raise

        self._output_name = self._monitor.name
        backend = resolve_capture_backend(self.capture_backend)
        self._capture = create_capture(
            self._monitor.index,
            backend=backend,
            screen_name=self._output_name,
            include_cursor=True,
        )
        return self._output_name

    def _wait_for_output(self, expected_name: str, timeout_seconds: float = 10.0) -> MonitorInfo:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self._service_proc is not None and self._service_proc.poll() is not None:
                stderr = ""
                if self._service_proc.stderr is not None:
                    stderr = self._service_proc.stderr.read().decode("utf-8", errors="replace").strip()
                raise RuntimeError(
                    f"krfb-virtualmonitor exited during startup{f': {stderr}' if stderr else ''}"
                )
            for monitor in list_monitors("spectacle"):
                if monitor.name == expected_name:
                    return monitor
            time.sleep(0.1)
        raise RuntimeError(
            f"timed out waiting for compositor output {expected_name!r}. "
            "Run inside a KDE Plasma Wayland session."
        )

    def launch(self, command: list[str] | str, *, shell: bool = False) -> subprocess.Popen:
        """Launch an app on the current desktop session."""

        if self._output_name is None:
            raise RuntimeError("start the compositor monitor before launching apps")

        process = subprocess.Popen(
            command,
            env=os.environ.copy(),
            shell=shell,
            start_new_session=True,
        )
        self._children.append(process)
        return process

    def _refresh_monitor_layout(self) -> None:
        if self._output_name is None:
            return
        for monitor in list_monitors("spectacle"):
            if monitor.name == self._output_name:
                self._monitor = monitor
                return

    def grab(self) -> Image.Image:
        """Capture the compositor monitor using the normal desktop capture stack."""

        if self._capture is None or self._output_name is None:
            raise RuntimeError("start the compositor monitor before grabbing frames")

        self._refresh_monitor_layout()
        image = self._capture.grab()
        rotation = kde_output_rotation(self._output_name)
        if rotation != self._last_rotation:
            if self._last_rotation is not None:
                print(f"compositor rotation changed: {self._last_rotation} -> {rotation}")
            self._last_rotation = rotation
        return apply_kde_rotation(image, rotation)

    def stop(self) -> None:
        """Close child apps and remove the compositor monitor."""

        for child in reversed(self._children):
            if child.poll() is None:
                child.send_signal(signal.SIGTERM)
        for child in reversed(self._children):
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
        self._children.clear()

        if self._capture is not None:
            self._capture.close()
        self._capture = None

        if self._service_proc is not None and self._service_proc.poll() is None:
            self._service_proc.send_signal(signal.SIGTERM)
            try:
                self._service_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._service_proc.kill()
        self._service_proc = None

        self._output_name = None
        self._monitor = None

    def __enter__(self) -> CompositorMonitor:
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()
