"""DRM helpers for the vkms virtual panel output."""

from __future__ import annotations

import ctypes
import ctypes.util
import fcntl
import json
import mmap
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

DRM_IOCTL_MODE_MAP_DUMB = 0xC01064B3
DRM_IOCTL_PRIME_HANDLE_TO_FD = 0xC010642D


class DrmModeMapDumb(ctypes.Structure):
    _fields_ = [
        ("handle", ctypes.c_uint32),
        ("pad", ctypes.c_uint32),
        ("offset", ctypes.c_uint64),
    ]


class DrmPrimeHandle(ctypes.Structure):
    _fields_ = [
        ("handle", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("fd", ctypes.c_int32),
    ]


class DrmModeModeInfo(ctypes.Structure):
    _fields_ = [
        ("clock", ctypes.c_uint32),
        ("hdisplay", ctypes.c_uint16),
        ("hsync_start", ctypes.c_uint16),
        ("hsync_end", ctypes.c_uint16),
        ("htotal", ctypes.c_uint16),
        ("hskew", ctypes.c_uint16),
        ("vdisplay", ctypes.c_uint16),
        ("vsync_start", ctypes.c_uint16),
        ("vsync_end", ctypes.c_uint16),
        ("vtotal", ctypes.c_uint16),
        ("vscan", ctypes.c_uint16),
        ("vrefresh", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("name", ctypes.c_char * 32),
    ]


class DrmModeConnectorC(ctypes.Structure):
    _fields_ = [
        ("connector_id", ctypes.c_uint32),
        ("encoder_id", ctypes.c_uint32),
        ("connector_type", ctypes.c_uint32),
        ("connector_type_id", ctypes.c_uint32),
        ("connection", ctypes.c_uint32),
        ("mm_width", ctypes.c_uint32),
        ("mm_height", ctypes.c_uint32),
        ("subpixel", ctypes.c_uint32),
        ("count_modes", ctypes.c_int),
        ("modes", ctypes.POINTER(DrmModeModeInfo)),
        ("count_props", ctypes.c_int),
        ("props", ctypes.POINTER(ctypes.c_uint32)),
        ("prop_values", ctypes.POINTER(ctypes.c_uint64)),
        ("count_encoders", ctypes.c_int),
        ("encoders", ctypes.POINTER(ctypes.c_uint32)),
    ]


class DrmModeEncoderC(ctypes.Structure):
    _fields_ = [
        ("encoder_id", ctypes.c_uint32),
        ("encoder_type", ctypes.c_uint32),
        ("crtc_id", ctypes.c_uint32),
        ("possible_crtcs", ctypes.c_uint32),
        ("possible_clones", ctypes.c_uint32),
    ]


class DrmModeCrtcC(ctypes.Structure):
    _fields_ = [
        ("crtc_id", ctypes.c_uint32),
        ("buffer_id", ctypes.c_uint32),
        ("x", ctypes.c_uint32),
        ("y", ctypes.c_uint32),
        ("width", ctypes.c_uint32),
        ("height", ctypes.c_uint32),
        ("mode_valid", ctypes.c_int),
        ("mode", DrmModeModeInfo),
    ]


class DrmModeFBC(ctypes.Structure):
    _fields_ = [
        ("fb_id", ctypes.c_uint32),
        ("width", ctypes.c_uint32),
        ("height", ctypes.c_uint32),
        ("pitch", ctypes.c_uint32),
        ("bpp", ctypes.c_uint32),
        ("depth", ctypes.c_uint32),
        ("handle", ctypes.c_uint32),
    ]


_libdrm = None


def _drm_ptr(struct_type: type[ctypes.Structure]):
    def _cast(ptr: int) -> ctypes.Structure | None:
        if not ptr:
            return None
        return ctypes.cast(ptr, ctypes.POINTER(struct_type)).contents

    return _cast


def _drm_lib():
    global _libdrm
    if _libdrm is not None:
        return _libdrm
    name = ctypes.util.find_library("drm")
    if not name:
        raise RuntimeError("libdrm is not installed (Fedora: sudo dnf install libdrm)")
    _libdrm = ctypes.CDLL(name, use_errno=True)
    _libdrm.drmModeGetConnector.argtypes = [ctypes.c_int, ctypes.c_uint32]
    _libdrm.drmModeGetConnector.restype = ctypes.c_void_p
    _libdrm.drmModeFreeConnector.argtypes = [ctypes.c_void_p]
    _libdrm.drmModeGetEncoder.argtypes = [ctypes.c_int, ctypes.c_uint32]
    _libdrm.drmModeGetEncoder.restype = ctypes.c_void_p
    _libdrm.drmModeFreeEncoder.argtypes = [ctypes.c_void_p]
    _libdrm.drmModeGetCrtc.argtypes = [ctypes.c_int, ctypes.c_uint32]
    _libdrm.drmModeGetCrtc.restype = ctypes.c_void_p
    _libdrm.drmModeFreeCrtc.argtypes = [ctypes.c_void_p]
    _libdrm.drmModeGetFB.argtypes = [ctypes.c_int, ctypes.c_uint32]
    _libdrm.drmModeGetFB.restype = ctypes.c_void_p
    _libdrm.drmModeFreeFB.argtypes = [ctypes.c_void_p]
    return _libdrm


def _drm_ioctl(fd: int, request: int, arg: ctypes.Structure) -> None:
    try:
        fcntl.ioctl(fd, request, arg)
    except OSError as exc:
        raise RuntimeError(f"DRM ioctl failed: {exc}") from exc


@dataclass(frozen=True)
class DrmOutput:
    """A DRM connector exposed by the vkms card."""

    card_path: Path
    connector_id: int
    connector_name: str
    width: int
    height: int


def _card_driver_name(card: Path) -> str | None:
    driver = card / "device" / "driver"
    if not driver.is_symlink():
        return None
    return driver.resolve().name


def _card_nodes() -> list[Path]:
    nodes: list[Path] = []
    for entry in sorted(Path("/sys/class/drm").glob("card[0-9]*")):
        if "-" in entry.name:
            continue
        nodes.append(entry)
    return nodes


def _is_vkms_card(card: Path) -> bool:
    """vkms exposes Virtual-* connectors and uses the faux platform bus."""

    if _card_driver_name(card) == "vkms":
        return True
    if _card_driver_name(card) != "faux_driver":
        return False
    return any(Path("/sys/class/drm").glob(f"{card.name}-Virtual-*"))


def ensure_vkms_loaded() -> None:
    """Load the in-kernel vkms DRM driver if it is not already present."""

    for card in _card_nodes():
        if _is_vkms_card(card):
            return

    result = subprocess.run(
        ["modprobe", "vkms"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(
            "failed to load vkms DRM module "
            f"(Fedora: sudo dnf install kernel-modules-extra; then sudo modprobe vkms): {stderr}"
        )

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        for card in _card_nodes():
            if _is_vkms_card(card):
                return
        time.sleep(0.1)
    raise RuntimeError("vkms loaded but no DRM card appeared")


def vkms_card_path() -> Path:
    """Return the /dev/dri/cardN path for the vkms device."""

    ensure_vkms_loaded()
    for card in _card_nodes():
        if _is_vkms_card(card):
            return Path("/dev/dri") / card.name
    raise RuntimeError("vkms DRM card not found")


def _connector_sysfs_name(card_name: str, connector_id: int) -> str | None:
    for entry in sorted(Path("/sys/class/drm").glob(f"{card_name}-*")):
        try:
            if int((entry / "connector_id").read_text(encoding="utf-8").strip()) == connector_id:
                return entry.name.removeprefix(f"{card_name}-")
        except (FileNotFoundError, ValueError):
            continue
    return None


def _vkms_connector_from_sysfs(card_name: str) -> tuple[int, str]:
    for entry in sorted(Path("/sys/class/drm").glob(f"{card_name}-Virtual-*")):
        connector_id = int((entry / "connector_id").read_text(encoding="utf-8").strip())
        return connector_id, entry.name.removeprefix(f"{card_name}-")
    raise RuntimeError("no vkms connector found")


def wait_for_vkms_connector(timeout_seconds: float = 15.0) -> tuple[int, str]:
    """Wait until vkms exposes a Virtual connector."""

    ensure_vkms_loaded()
    card_name = vkms_card_path().name
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            return _vkms_connector_from_sysfs(card_name)
        except RuntimeError:
            time.sleep(0.2)
    raise RuntimeError("timed out waiting for vkms connector")


def _kscreen_outputs() -> list[dict]:
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
    outputs = payload.get("outputs")
    return outputs if isinstance(outputs, list) else []


def _find_kscreen_output(name_hint: str) -> dict | None:
    outputs = _kscreen_outputs()
    lowered = name_hint.lower()
    for output in outputs:
        name = str(output.get("name") or "")
        if name.lower() == lowered:
            return output
    for output in outputs:
        name = str(output.get("name") or "")
        if "virtual" in name.lower() or name.lower().startswith("vkms"):
            return output
    return None


def _run_kscreen(*commands: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["kscreen-doctor", *commands],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def _modes_for_size(output: dict, width: int, height: int) -> list[dict]:
    matches: list[dict] = []
    for mode in output.get("modes", []):
        size = mode.get("size") or {}
        if int(size.get("width", 0)) == width and int(size.get("height", 0)) == height:
            matches.append(mode)
    return matches


def _mode_hz(mode: dict) -> float:
    return float(mode.get("refreshRate", 0.0))


def _select_panel_mode_id(
    output: dict,
    width: int,
    height: int,
    target_hz: float | None,
) -> str | None:
    """Pick the panel-resolution mode that best avoids throttling the desktop.

    ``target_hz`` is the refresh rate we want to reach or exceed (typically the
    fastest real monitor). We choose the slowest mode that is still at or above
    that target so KWin's shared animation clock is not dragged below the other
    outputs. When ``target_hz`` is ``None`` or no mode reaches it, the highest
    available refresh rate is used.
    """

    modes = _modes_for_size(output, width, height)
    if not modes:
        return None
    if target_hz is None:
        best = max(modes, key=_mode_hz)
    else:
        at_or_above = [mode for mode in modes if _mode_hz(mode) >= target_hz - 0.5]
        best = min(at_or_above, key=_mode_hz) if at_or_above else max(modes, key=_mode_hz)
    mode_id = best.get("id")
    return str(mode_id) if mode_id is not None else None


def _max_other_output_refresh_hz(panel_name: str) -> float:
    """Highest current refresh among the other enabled outputs (real monitors)."""

    panel = panel_name.lower()
    best = 0.0
    for output in _kscreen_outputs():
        name = str(output.get("name") or "")
        if name.lower() == panel or not output.get("enabled"):
            continue
        current = str(output.get("currentModeId", ""))
        for mode in output.get("modes", []):
            if str(mode.get("id")) == current:
                best = max(best, _mode_hz(mode))
    return best


def _resolve_target_hz(refresh: str | float, panel_name: str) -> float | None:
    """Translate a refresh spec ("match"/"max"/<hz>) into a target refresh.

    Returns ``None`` to mean "use the highest mode available".
    """

    if isinstance(refresh, (int, float)):
        return float(refresh)
    spec = str(refresh).strip().lower()
    if spec == "max":
        return None
    if spec == "match":
        fastest = _max_other_output_refresh_hz(panel_name)
        return fastest if fastest > 0 else None
    try:
        return float(spec)
    except ValueError:
        return None


def _clear_all_custom_modes(output_name: str) -> None:
    """Remove custom modes accumulated from earlier addCustomMode calls."""

    for _ in range(64):
        result = _run_kscreen(f"output.{output_name}.removeCustomMode.0")
        if result.returncode != 0:
            return
        time.sleep(0.05)


def reload_vkms_card() -> None:
    """Reload vkms to drop duplicate panel modes left by earlier custom-mode calls."""

    modprobe = shutil.which("modprobe")
    if modprobe is None:
        raise RuntimeError("modprobe not found; cannot reload vkms")

    prefixes: tuple[list[str], ...] = ([], ["sudo", "-n"])
    last_error = "failed to reload vkms"
    for prefix in prefixes:
        ok = True
        for args in (["-r", "vkms"], ["vkms"]):
            result = subprocess.run(
                [*prefix, modprobe, *args],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                stderr = (result.stderr or result.stdout or "").strip()
                last_error = f"{' '.join([*prefix, modprobe, *args])} failed{f': {stderr}' if stderr else ''}"
                ok = False
                break
        if not ok:
            continue

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if any(Path("/sys/class/drm").glob("card*-Virtual-*")):
                return
            time.sleep(0.1)
        return

    raise RuntimeError(
        f"{last_error}. "
        "Re-run ./scripts/install.sh to allow passwordless vkms reload, or run: "
        "sudo modprobe -r vkms && sudo modprobe vkms"
    )


def _current_mode_size(output: dict) -> tuple[int, int]:
    current_mode = str(output.get("currentModeId", ""))
    for mode in output.get("modes", []):
        if str(mode.get("id")) == current_mode:
            size = mode.get("size") or {}
            return int(size.get("width", 0)), int(size.get("height", 0))
    size = output.get("size") or {}
    return int(size.get("width", 0)), int(size.get("height", 0))


def _ensure_panel_mode_id(
    output_name: str,
    width: int,
    height: int,
    *,
    target_hz: float | None = None,
    refresh_millihz: int = 60_000,
) -> str:
    """Return the kscreen mode id for the panel resolution at the wanted refresh.

    Existing modes are reused so we do not keep adding custom modes (and so a
    high-refresh mode that keeps the desktop smooth is preserved). A custom mode
    is only created when the connector has no mode at the panel resolution yet.
    """

    output = _find_kscreen_output(output_name)
    if output is None:
        raise RuntimeError(f"kscreen output {output_name!r} disappeared")

    mode_id = _select_panel_mode_id(output, width, height, target_hz)
    if mode_id is not None:
        return mode_id

    result = _run_kscreen(
        f"output.{output_name}.addCustomMode.{width}.{height}.{refresh_millihz}.full"
    )
    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(
            f"failed to add {width}x{height} custom mode for {output_name}"
            f"{f': {stderr}' if stderr else ''}"
        )
    time.sleep(0.3)
    output = _find_kscreen_output(output_name)
    if output is None:
        raise RuntimeError(f"kscreen output {output_name!r} disappeared after addCustomMode")
    mode_id = _select_panel_mode_id(output, width, height, target_hz)
    if mode_id is None:
        raise RuntimeError(
            f"kscreen has no {width}x{height} mode for {output_name}. "
            "Plasma 6.6+ is required for custom modes on vkms."
        )
    return mode_id


def _wait_for_kscreen_output(name_hint: str, timeout_seconds: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        output = _find_kscreen_output(name_hint)
        if output is not None:
            return output
        time.sleep(0.2)
    raise RuntimeError(
        f"timed out waiting for kscreen output {name_hint!r}. "
        "Run inside a KDE Plasma Wayland session."
    )


def enable_kscreen_output(
    connector_name: str,
    width: int,
    height: int,
    *,
    refresh: str | float = "match",
    refresh_millihz: int = 60_000,
) -> str:
    """Enable the vkms connector in KDE and return the kscreen output name.

    ``refresh`` controls the panel mode picked for the virtual output:

    * ``"match"`` (default): track the fastest real monitor so KWin's shared
      animation clock is not throttled to the panel's rate.
    * ``"max"``: use the highest refresh mode the connector advertises.
    * a number: target that refresh rate in Hz.

    Screen position is left to KScreen/Plasma so the layout you set in Display
    Settings is remembered across reboots like any other monitor.
    """

    if not shutil.which("kscreen-doctor"):
        return connector_name

    output = _wait_for_kscreen_output(connector_name)
    output_name = str(output.get("name") or connector_name)
    target_hz = _resolve_target_hz(refresh, output_name)
    mode_id = _ensure_panel_mode_id(
        output_name,
        width,
        height,
        target_hz=target_hz,
        refresh_millihz=refresh_millihz,
    )

    result = _run_kscreen(
        f"output.{output_name}.enable",
        f"output.{output_name}.mode.{mode_id}",
    )
    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(
            f"kscreen-doctor failed to enable {output_name}"
            f"{f': {stderr}' if stderr else ''}"
        )

    return output_name


def _wait_for_active_framebuffer(
    card_path: Path,
    connector_id: int,
    timeout_seconds: float = 10.0,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error = "vkms connector has no active framebuffer"
    while time.monotonic() < deadline:
        fd = os.open(str(card_path), os.O_RDWR | os.O_CLOEXEC)
        try:
            _active_crtc_fb(fd, connector_id)
            return
        except RuntimeError as exc:
            last_error = str(exc)
        finally:
            os.close(fd)
        time.sleep(0.2)
    raise RuntimeError(last_error)


def _active_crtc_fb(fd: int, connector_id: int) -> int:
    lib = _drm_lib()
    cast_connector = _drm_ptr(DrmModeConnectorC)
    cast_encoder = _drm_ptr(DrmModeEncoderC)
    cast_crtc = _drm_ptr(DrmModeCrtcC)

    connector_ptr = lib.drmModeGetConnector(fd, connector_id)
    connector = cast_connector(connector_ptr)
    if connector is None:
        raise RuntimeError(f"drmModeGetConnector({connector_id}) failed")
    try:
        encoder_id = connector.encoder_id
        if not encoder_id:
            raise RuntimeError("vkms connector has no active encoder")
        encoder_ptr = lib.drmModeGetEncoder(fd, encoder_id)
        encoder = cast_encoder(encoder_ptr)
        if encoder is None:
            raise RuntimeError(f"drmModeGetEncoder({encoder_id}) failed")
        try:
            crtc_id = encoder.crtc_id
            if not crtc_id:
                raise RuntimeError("vkms encoder has no active CRTC")
            crtc_ptr = lib.drmModeGetCrtc(fd, crtc_id)
            crtc = cast_crtc(crtc_ptr)
            if crtc is None:
                raise RuntimeError(f"drmModeGetCrtc({crtc_id}) failed")
            try:
                if not crtc.buffer_id:
                    raise RuntimeError("vkms CRTC has no active framebuffer")
                return int(crtc.buffer_id)
            finally:
                lib.drmModeFreeCrtc(crtc_ptr)
        finally:
            lib.drmModeFreeEncoder(encoder_ptr)
    finally:
        lib.drmModeFreeConnector(connector_ptr)


def _map_framebuffer(fd: int, handle: int, size: int) -> mmap.mmap:
    map_arg = DrmModeMapDumb(handle=handle)
    try:
        _drm_ioctl(fd, DRM_IOCTL_MODE_MAP_DUMB, map_arg)
        return mmap.mmap(fd, size, mmap.MAP_SHARED, mmap.PROT_READ, offset=map_arg.offset)
    except RuntimeError:
        prime = DrmPrimeHandle(handle=handle, flags=0, fd=-1)
        _drm_ioctl(fd, DRM_IOCTL_PRIME_HANDLE_TO_FD, prime)
        try:
            return mmap.mmap(prime.fd, size, mmap.MAP_SHARED, mmap.PROT_READ)
        finally:
            os.close(prime.fd)


def capture_framebuffer(card_path: Path, connector_id: int) -> Image.Image:
    """Capture the current vkms framebuffer as an RGB image."""

    fd = os.open(str(card_path), os.O_RDWR | os.O_CLOEXEC)
    try:
        fb_id = _active_crtc_fb(fd, connector_id)
        lib = _drm_lib()
        cast_fb = _drm_ptr(DrmModeFBC)
        fb_ptr = lib.drmModeGetFB(fd, fb_id)
        fb = cast_fb(fb_ptr)
        if fb is None:
            raise RuntimeError(f"drmModeGetFB({fb_id}) failed")
        try:
            handle = int(fb.handle)
            width = int(fb.width)
            height = int(fb.height)
            pitch = int(fb.pitch)
            size = pitch * height
        finally:
            lib.drmModeFreeFB(fb_ptr)

        if not handle:
            raise RuntimeError("vkms framebuffer handle unavailable (compositor holds DRM master)")

        mapping = _map_framebuffer(fd, handle, size)
        try:
            raw = bytearray(mapping[:size])
        finally:
            mapping.close()
    finally:
        os.close(fd)

    if width <= 0 or height <= 0:
        raise RuntimeError("vkms framebuffer has invalid dimensions")

    pixels = bytearray(width * height * 3)
    for y in range(height):
        src = y * pitch
        dst = y * width * 3
        for x in range(width):
            offset = src + x * 4
            pixels[dst + x * 3 : dst + x * 3 + 3] = raw[offset : offset + 3]
    return Image.frombytes("RGB", (width, height), bytes(pixels))


def prepare_drm_output(
    width: int,
    height: int,
    *,
    refresh: str | float = "match",
) -> DrmOutput:
    """Load vkms, wait for a connector, and enable it through kscreen."""

    ensure_vkms_loaded()
    card_path = vkms_card_path()
    connector_id, connector_sysfs = wait_for_vkms_connector()
    output_name = enable_kscreen_output(connector_sysfs, width, height, refresh=refresh)
    _wait_for_active_framebuffer(card_path, connector_id)
    actual_width, actual_height = width, height
    output = _find_kscreen_output(output_name)
    if output is not None:
        mode_width, mode_height = _current_mode_size(output)
        if mode_width > 0 and mode_height > 0:
            actual_width, actual_height = mode_width, mode_height
    return DrmOutput(
        card_path=card_path,
        connector_id=connector_id,
        connector_name=output_name,
        width=actual_width,
        height=actual_height,
    )
