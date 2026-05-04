#!/usr/bin/env python3
"""Small documented userspace driver for the ArtInChip USB display.

Tested with the eM3499-Monitor / ArtInChip USB Display device:

    VID:PID 33c3:0e02
    Product eM3499-Monitor
    Serial  2024123456
    Mode    480x480, JPEG media format 0x10

The display endpoint accepts complete JPEG frames preceded by a 20-byte
little-endian ArtInChip frame header. Before frames are accepted, the host must
complete the two-step RSA challenge/response handshake implemented below.

This module intentionally avoids any vendor binary library. It uses PyUSB for
the vendor bulk display interface and hidapi for the optional touch interface.
"""

from __future__ import annotations

import io
import os
import random
import struct
import subprocess
import sys
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


class ArtInChipDisplay:
    """A direct USB connection to the ArtInChip display bulk interface."""

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
            # macOS does not expose detach_kernel_driver through libusb.
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

    def write_all(self, data: bytes, timeout: int = 1000):
        """Write a buffer to the bulk OUT endpoint in bounded chunks."""

        pos = 0
        while pos < len(data):
            pos += self.ep_out.write(data[pos : pos + self.chunk_size], timeout)

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
        response = bytes(self.ep_in.read(256, timeout=1000))
        if response != challenge:
            raise RuntimeError("device authentication failed")

        # Phase 2: device sends an RSA type-1 padded block. The host performs
        # public-key recovery and returns the clear payload.
        self.send_command(AUTH_HOST_CMD)
        signature = bytes(self.ep_in.read(256, timeout=1000))
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


def print_platform_hints():
    """Print short actionable hints for common permission problems."""

    if sys.platform == "darwin":
        print("macOS hint: install libusb with Homebrew and run from a venv.")
        print("  brew install libusb hidapi")
    elif sys.platform.startswith("linux"):
        print("Linux hint: add the udev rule from scripts/99-artinchip-usb-display.rules")
        print("or run the test once with sudo to confirm it is a permissions issue.")


def nc_http_post(host: str, port: int, request: bytes, timeout_seconds: int = 5) -> bytes:
    """Tiny helper used by other projects when Python sockets cannot route.

    It is not needed for display transport. It documents the practical fallback
    used during the information-screen work for a local llama.cpp endpoint.
    """

    result = subprocess.run(
        ["nc", "-w", str(timeout_seconds), host, str(port)],
        input=request,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout_seconds + 2,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace").strip())
    return result.stdout
