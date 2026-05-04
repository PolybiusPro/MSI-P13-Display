#!/usr/bin/env python3
"""Show a clock on an ArtInChip USB Display monitor.

Tested against eM3499-Monitor / ArtInChip VID:PID 33c3:0e02.
Requires pyusb, pillow and cryptography in the local venv.
"""

from __future__ import annotations

import argparse
import io
import os
import random
import struct
import time
from dataclasses import dataclass

import usb.core
import usb.util
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
from PIL import Image, ImageDraw, ImageFont


VID = 0x33C3
PID = 0x0E02

VENDOR_CMD0_GET_PARAMETER = 0
VENDOR_CMD12_HEARTBEAT = 12
FRAME_START_MAGIC = 0xA1C62B01
AUTH_DEV_CMD = 0xA1C62B10
AUTH_HOST_CMD = 0xA1C62B11

PIXEL_ENCODE_JPEG = 0x10

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
    version: int
    chipid: int
    media_format: int
    media_bus: int
    mode_num: int
    width: int
    height: int
    fps: int


def load_public_key():
    key = serialization.load_pem_public_key(PUBKEY_PEM)
    numbers = key.public_numbers()
    size = (numbers.n.bit_length() + 7) // 8
    return key, numbers.n, numbers.e, size


def public_decrypt_pkcs1_type1(signature: bytes, n: int, e: int, size: int) -> bytes:
    block = pow(int.from_bytes(signature, "big"), e, n).to_bytes(size, "big")
    if len(block) != size or block[:2] != b"\x00\x01":
        raise RuntimeError(f"bad RSA block header: {block[:16].hex()}")

    sep = block.find(b"\x00", 2)
    if sep < 0:
        raise RuntimeError("bad RSA block: no separator")
    if any(byte != 0xFF for byte in block[2:sep]):
        raise RuntimeError("bad RSA block padding")
    return block[sep + 1 :]


def open_device(vid: int, pid: int):
    dev = usb.core.find(idVendor=vid, idProduct=pid)
    if dev is None:
        raise RuntimeError(f"USB display {vid:04x}:{pid:04x} not found")

    try:
        dev.set_configuration()
    except usb.core.USBError:
        pass

    try:
        if dev.is_kernel_driver_active(0):
            dev.detach_kernel_driver(0)
    except (NotImplementedError, usb.core.USBError):
        pass

    usb.util.claim_interface(dev, 0)
    cfg = dev.get_active_configuration()
    intf = cfg[(0, 0)]
    ep_out = usb.util.find_descriptor(
        intf,
        custom_match=lambda ep: usb.util.endpoint_direction(ep.bEndpointAddress)
        == usb.util.ENDPOINT_OUT,
    )
    ep_in = usb.util.find_descriptor(
        intf,
        custom_match=lambda ep: usb.util.endpoint_direction(ep.bEndpointAddress)
        == usb.util.ENDPOINT_IN,
    )
    if ep_out is None or ep_in is None:
        raise RuntimeError("bulk endpoints not found on interface 0")
    return dev, ep_out, ep_in


def get_params(dev) -> DeviceParams:
    raw = bytes(
        dev.ctrl_transfer(
            0xC0,
            VENDOR_CMD0_GET_PARAMETER,
            0,
            0,
            160,
            timeout=1000,
        )
    )
    values = struct.unpack_from("<HHHHHHHH", raw)
    return DeviceParams(*values)


def heartbeat(dev, value: int, verbose: bool = False) -> bool:
    try:
        response = bytes(dev.ctrl_transfer(0xC0, VENDOR_CMD12_HEARTBEAT, value, 0, 1, timeout=1000))
        if verbose:
            print(f"heartbeat value={value} response={response.hex()}")
        return True
    except usb.core.USBError as exc:
        if verbose:
            print(f"heartbeat value={value} failed: {exc}")
        return False


def write_all(ep_out, data: bytes, timeout: int = 1000, chunk_size: int = 64 * 1024):
    pos = 0
    while pos < len(data):
        pos += ep_out.write(data[pos : pos + chunk_size], timeout)


def send_command(ep_out, command: int, chunk_size: int = 64 * 1024):
    packet = struct.pack("<IIHHII", command, 256, 0, 0, 0, command)
    write_all(ep_out, packet, chunk_size=chunk_size)


def authenticate(ep_out, ep_in, verbose: bool = False, chunk_size: int = 64 * 1024):
    key, n, e, rsa_size = load_public_key()

    challenge = os.urandom(random.randint(1, 244))
    encrypted = key.encrypt(challenge, padding.PKCS1v15())
    send_command(ep_out, AUTH_DEV_CMD, chunk_size=chunk_size)
    write_all(ep_out, encrypted, chunk_size=chunk_size)
    response = bytes(ep_in.read(256, timeout=1000))
    if response != challenge:
        raise RuntimeError(
            f"device auth failed: expected {len(challenge)} bytes, got {len(response)}"
        )
    if verbose:
        print(f"auth_dev ok ({len(challenge)} bytes)")

    send_command(ep_out, AUTH_HOST_CMD, chunk_size=chunk_size)
    signature = bytes(ep_in.read(256, timeout=1000))
    clear = public_decrypt_pkcs1_type1(signature, n, e, rsa_size)
    write_all(ep_out, clear, chunk_size=chunk_size)
    if verbose:
        print(f"auth_host ok ({len(clear)} bytes)")


def choose_font(size: int):
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def make_clock_jpeg(
    width: int,
    height: int,
    text: str,
    quality: int,
    include_date: bool,
    subsampling: int,
) -> bytes:
    img = Image.new("RGB", (width, height), (4, 6, 10))
    draw = ImageDraw.Draw(img)

    font = choose_font(max(24, min(width, height) // 5))
    small = choose_font(max(14, min(width, height) // 18))
    box = draw.textbbox((0, 0), text, font=font)
    tw = box[2] - box[0]
    th = box[3] - box[1]

    draw.rectangle((0, 0, width - 1, height - 1), outline=(0, 170, 255), width=8)
    draw.text(((width - tw) // 2, (height - th) // 2 - 8), text, fill=(255, 255, 255), font=font)
    if include_date:
        date_text = time.strftime("%Y-%m-%d")
        date_box = draw.textbbox((0, 0), date_text, font=small)
        draw.text(
            ((width - (date_box[2] - date_box[0])) // 2, (height + th) // 2 + 28),
            date_text,
            fill=(0, 190, 255),
            font=small,
        )

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=quality, subsampling=subsampling, progressive=False)
    return out.getvalue()


def send_jpeg_frame(ep_out, jpeg: bytes, frame_id: int, chunk_size: int):
    header = struct.pack(
        "<IIHHII",
        FRAME_START_MAGIC,
        len(jpeg),
        frame_id & 0xFFFF,
        PIXEL_ENCODE_JPEG,
        0,
        FRAME_START_MAGIC,
    )
    write_all(ep_out, header, chunk_size=chunk_size)
    write_all(ep_out, jpeg, chunk_size=chunk_size)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vid", type=lambda s: int(s, 0), default=VID)
    parser.add_argument("--pid", type=lambda s: int(s, 0), default=PID)
    parser.add_argument("--duration", type=float, default=0, help="seconds; 0 means forever")
    parser.add_argument("--frames", type=int, default=0, help="frame count; 0 means use duration")
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between frames")
    parser.add_argument("--quality", type=int, default=75)
    parser.add_argument(
        "--subsampling",
        type=int,
        default=2,
        choices=(0, 1, 2),
        help="Pillow JPEG subsampling: 0=4:4:4, 1=4:2:2, 2=4:2:0",
    )
    parser.add_argument("--chunk-size", type=int, default=16 * 1024)
    parser.add_argument("--heartbeat", action="store_true", help="try vendor heartbeat request 12")
    parser.add_argument("--no-date", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    dev, ep_out, ep_in = open_device(args.vid, args.pid)
    params = get_params(dev)
    if args.verbose:
        print(params)
    if params.media_format != PIXEL_ENCODE_JPEG:
        raise RuntimeError(f"only JPEG mode is implemented, got format 0x{params.media_format:x}")

    authenticate(ep_out, ep_in, verbose=args.verbose, chunk_size=args.chunk_size)
    heartbeat_enabled = False
    if args.heartbeat:
        heartbeat_enabled = heartbeat(dev, 1, verbose=args.verbose)

    start = time.monotonic()
    frame_id = 0
    try:
        while not args.duration or time.monotonic() - start < args.duration:
            if args.frames and frame_id >= args.frames:
                break
            if heartbeat_enabled:
                heartbeat(dev, 0, verbose=False)
            text = time.strftime("%H:%M:%S")
            jpeg = make_clock_jpeg(
                params.width,
                params.height,
                text,
                args.quality,
                not args.no_date,
                args.subsampling,
            )
            send_jpeg_frame(ep_out, jpeg, frame_id, args.chunk_size)
            if args.verbose:
                print(f"sent frame {frame_id}: {text}, {len(jpeg)} bytes")
            frame_id += 1
            time.sleep(args.interval)
    finally:
        if heartbeat_enabled:
            heartbeat(dev, 2, verbose=args.verbose)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
