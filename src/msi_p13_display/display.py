#!/usr/bin/env python3
"""Userspace driver for the MSI P13 USB display panel.

Tested with the MSI P13 display (ArtInChip USB controller):

    VID:PID 33c3:0e02
    Product MSI P13 / ArtInChip USB Display
    Serial  2024123456
    Mode    480x480, JPEG media format 0x10

The display endpoint accepts complete JPEG frames preceded by a 20-byte
little-endian ArtInChip frame header. Before frames are accepted, the host must
complete the two-step RSA challenge/response handshake implemented below.

This module intentionally avoids any vendor binary library. It uses PyUSB for
the vendor bulk display interface.
"""

from __future__ import annotations

import io
import os
import random
import struct
from dataclasses import dataclass

import usb.core
import usb.util
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
from PIL import Image


VID = 0x33C3
PID = 0x0E02

VENDOR_CMD0_GET_PARAMETER = 0
FRAME_START_MAGIC = 0xA1C62B01
AUTH_DEV_CMD = 0xA1C62B10
AUTH_HOST_CMD = 0xA1C62B11
PIXEL_ENCODE_JPEG = 0x10

# This public key was recovered from the ArtInChip aic-render binary. It is
# used for the device-side challenge and to verify/decrypt the host challenge.
PUBKEY_PEM = b"""-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAybdtvB1uNA4XICh+xJi1
KJWO0GYal4lNiW69zSMIJFGzb2wkiFBX2txFaH5ZYh0TYdwmjzBqinzTsWhIasW3
rl9QN5cv73zFalO3J4hADXz1g7hlHVB0BKDD280NUKUGAbwDv+KMHTprs+B/T4QU
a0s4RBNnN4fMPk2H0UAWU1jKAvMYjh/YR+MLYbl04ZCLlOfX9zQjRBVan7aLARQg
v5QRahAlAoBsYK864VrBKq91lRCXt4XP5d/sDtZM7kGcpLi2i4xHtRct37M+bkZv
Lf/3aVpAVsqZy5P2NXEe6HMv4Q+YP6QKz2wuk3xWYHWFn+88ydjv394tN28rjl56
hwIDAQAB
-----END PUBLIC KEY-----
"""


@dataclass(frozen=True)
class DeviceParams:
    """Parameters returned by vendor control request 0."""

    version: int
    chipid: int
    media_format: int
    media_bus: int
    mode_num: int
    width: int
    height: int
    fps: int


class UsbDeviceLostError(RuntimeError):
    """Raised when the USB panel disconnects during use."""


def is_device_gone_error(exc: BaseException) -> bool:
    """Return True for errors that mean the USB panel is unplugged or unavailable."""

    if isinstance(exc, usb.core.USBError):
        # 19=ENODEV, 5=EIO, 32=EPIPE
        return exc.errno in {19, 5, 32}
    message = str(exc).lower()
    return "not found" in message or "no such device" in message or "disconnected" in message


class MsiP13Display:
    """A direct USB connection to the MSI P13 display bulk interface."""

    def __init__(self, vid: int = VID, pid: int = PID, chunk_size: int = 4096):
        self.vid = vid
        self.pid = pid
        self.chunk_size = chunk_size
        self.dev = None
        self.ep_out = None
        self.ep_in = None

    def open(self) -> DeviceParams:
        """Find the device, claim interface 0, authenticate, and return mode."""

        self.dev = usb.core.find(idVendor=self.vid, idProduct=self.pid)
        if self.dev is None:
            raise RuntimeError(f"USB display {self.vid:04x}:{self.pid:04x} not found")

        try:
            self.dev.set_configuration()
        except usb.core.USBError:
            # The active configuration may already be set by the OS.
            pass

        try:
            if self.dev.is_kernel_driver_active(0):
                self.dev.detach_kernel_driver(0)
        except (NotImplementedError, usb.core.USBError):
            pass

        usb.util.claim_interface(self.dev, 0)
        cfg = self.dev.get_active_configuration()
        intf = cfg[(0, 0)]
        self.ep_out = usb.util.find_descriptor(
            intf,
            custom_match=lambda ep: usb.util.endpoint_direction(ep.bEndpointAddress)
            == usb.util.ENDPOINT_OUT,
        )
        self.ep_in = usb.util.find_descriptor(
            intf,
            custom_match=lambda ep: usb.util.endpoint_direction(ep.bEndpointAddress)
            == usb.util.ENDPOINT_IN,
        )
        if self.ep_out is None or self.ep_in is None:
            raise RuntimeError("bulk endpoints not found on interface 0")

        params = self.get_params()
        if params.media_format != PIXEL_ENCODE_JPEG:
            raise RuntimeError(f"only JPEG mode is implemented, got 0x{params.media_format:x}")
        self.authenticate()
        return params

    def close(self):
        """Release the claimed USB interface if possible."""

        if self.dev is not None:
            try:
                usb.util.release_interface(self.dev, 0)
            except Exception:
                pass
        self.dev = None
        self.ep_out = None
        self.ep_in = None

    def _raise_if_disconnected(self, exc: BaseException) -> None:
        if is_device_gone_error(exc):
            raise UsbDeviceLostError("USB display disconnected") from exc
        raise exc

    def get_params(self) -> DeviceParams:
        """Read the 16-byte device mode block with vendor control request 0."""

        raw = bytes(
            self.dev.ctrl_transfer(
                0xC0,
                VENDOR_CMD0_GET_PARAMETER,
                0,
                0,
                160,
                timeout=1000,
            )
        )
        return DeviceParams(*struct.unpack_from("<HHHHHHHH", raw))

    def write_all(self, data: bytes, timeout: int = 2000):
        """Write a buffer to the bulk OUT endpoint in bounded chunks."""

        pos = 0
        while pos < len(data):
            try:
                pos += self.ep_out.write(data[pos : pos + self.chunk_size], timeout)
            except usb.core.USBError as exc:
                self._raise_if_disconnected(exc)

    def send_command(self, command: int):
        """Send an authentication command using the same 20-byte header shape."""

        packet = struct.pack("<IIHHII", command, 256, 0, 0, 0, command)
        self.write_all(packet)

    def authenticate(self):
        """Run the two RSA challenge/response phases required by the firmware."""

        key = serialization.load_pem_public_key(PUBKEY_PEM)
        numbers = key.public_numbers()
        rsa_size = (numbers.n.bit_length() + 7) // 8

        # Phase 1: host sends an RSA-encrypted random challenge; device returns
        # the original clear bytes, proving it can decrypt the block.
        challenge = os.urandom(random.randint(1, 244))
        encrypted = key.encrypt(challenge, padding.PKCS1v15())
        self.send_command(AUTH_DEV_CMD)
        self.write_all(encrypted)
        try:
            response = bytes(self.ep_in.read(256, timeout=1000))
        except usb.core.USBError as exc:
            self._raise_if_disconnected(exc)
        if response != challenge:
            raise RuntimeError("device authentication failed")

        # Phase 2: device sends an RSA type-1 padded block. The host performs
        # public-key recovery and returns the clear payload.
        self.send_command(AUTH_HOST_CMD)
        try:
            signature = bytes(self.ep_in.read(256, timeout=1000))
        except usb.core.USBError as exc:
            self._raise_if_disconnected(exc)
        clear = public_decrypt_pkcs1_type1(signature, numbers.n, numbers.e, rsa_size)
        self.write_all(clear)

    def send_jpeg(self, jpeg: bytes, frame_id: int = 0):
        """Send one complete JPEG image to the display."""

        header = struct.pack(
            "<IIHHII",
            FRAME_START_MAGIC,
            len(jpeg),
            frame_id & 0xFFFF,
            PIXEL_ENCODE_JPEG,
            0,
            FRAME_START_MAGIC,
        )
        self.write_all(header)
        self.write_all(jpeg)

    def send_image(self, image: Image.Image, frame_id: int = 0, quality: int = 60, subsampling: int = 2):
        """Encode a Pillow image as baseline JPEG and send it as a frame."""

        out = io.BytesIO()
        image.convert("RGB").save(
            out,
            format="JPEG",
            quality=quality,
            subsampling=subsampling,
            progressive=False,
        )
        self.send_jpeg(out.getvalue(), frame_id)


def public_decrypt_pkcs1_type1(signature: bytes, n: int, e: int, size: int) -> bytes:
    """Recover the payload from an RSA PKCS#1 v1.5 type-1 signature block."""

    block = pow(int.from_bytes(signature, "big"), e, n).to_bytes(size, "big")
    if len(block) != size or block[:2] != b"\x00\x01":
        raise RuntimeError(f"bad RSA block header: {block[:16].hex()}")
    sep = block.find(b"\x00", 2)
    if sep < 0:
        raise RuntimeError("bad RSA block: no separator")
    if any(byte != 0xFF for byte in block[2:sep]):
        raise RuntimeError("bad RSA block padding")
    return block[sep + 1 :]


def print_platform_hints(*, file=None):
    """Print short actionable hints for common permission problems."""

    print("Linux hint: add the udev rule from scripts/99-msi-p13-display.rules", file=file)
    print("or run the test once with sudo to confirm it is a permissions issue.", file=file)

