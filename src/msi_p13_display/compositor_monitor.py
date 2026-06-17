"""Register a vkms DRM output for the panel and stream it over USB."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
from dataclasses import dataclass, field

from PIL import Image

from msi_p13_display.capture import (
    CaptureBackend,
    apply_kde_rotation,
    create_capture,
    kde_output_rotation,
)
from msi_p13_display.drm_io import DrmOutput, prepare_drm_output


def is_wayland_session() -> bool:
    if os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
        return True
    return bool(os.environ.get("WAYLAND_DISPLAY"))


def tool_available(name: str) -> bool:
    return shutil.which(name) is not None


def default_shell() -> str | None:
    for candidate in ("konsole", "foot", "alacritty", "kitty", "wezterm", "ghostty", "xterm"):
        if tool_available(candidate):
            return candidate
    return None


@dataclass
class CompositorMonitor:
    """A vkms DRM output that KDE exposes as its own monitor."""

    width: int
    height: int
    _drm: DrmOutput | None = field(default=None, init=False, repr=False)
    _children: list[subprocess.Popen] = field(default_factory=list, init=False, repr=False)
    _capture: CaptureBackend | None = field(default=None, init=False, repr=False)
    _last_rotation: int | None = field(default=None, init=False, repr=False)

    @property
    def output_name(self) -> str:
        if self._drm is None:
            raise RuntimeError("compositor monitor is not started")
        return self._drm.connector_name

    def start(self) -> str:
        """Add a DRM virtual monitor and return its kscreen output name."""

        if self._drm is not None:
            return self._drm.connector_name

        if not is_wayland_session():
            raise RuntimeError("panel_monitor requires a Wayland session.")

        self._drm = prepare_drm_output(self.width, self.height)
        self._capture = create_capture(self._drm)
        return self.output_name

    def launch(self, command: list[str] | str, *, shell: bool = False) -> subprocess.Popen:
        """Launch an app on the current desktop session."""

        if self._drm is None:
            raise RuntimeError("start the compositor monitor before launching apps")

        process = subprocess.Popen(
            command,
            env=os.environ.copy(),
            shell=shell,
            start_new_session=True,
        )
        self._children.append(process)
        return process

    def grab(self) -> Image.Image:
        """Capture the vkms virtual monitor."""

        if self._capture is None or self._drm is None:
            raise RuntimeError("start the compositor monitor before grabbing frames")

        image = self._capture.grab()
        rotation = kde_output_rotation(self.output_name)
        if rotation != self._last_rotation:
            if self._last_rotation is not None:
                print(f"compositor rotation changed: {self._last_rotation} -> {rotation}")
            self._last_rotation = rotation
        return apply_kde_rotation(image, rotation)

    def stop(self) -> None:
        """Close child apps."""

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
        self._drm = None

    def __enter__(self) -> CompositorMonitor:
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()
