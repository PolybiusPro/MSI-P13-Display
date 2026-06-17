# MSI-P13-Display

Linux userspace driver for the MSI P13 USB display panel (ArtInChip controller).

![MSI P13 USB display panel](docs/assets/msi-p13-display.webp)

```text
VID:PID       33c3:0e02
Resolution    480x480
Media format  JPEG, 0x10
FPS           60
```

## Requirements

- Linux with libusb
- KDE Plasma Wayland for the panel monitor
- `vkms` DRM module (`kernel-modules-extra` on Fedora)
- Native packages: `python3-pillow`, `python3-cryptography`, `python3-pyusb`, `libdrm`, `kscreen`

The panel monitor uses the in-kernel **vkms** virtual display. Under Plasma Wayland, frames are captured via KWin ScreenShot2 because the vkms DRM framebuffer is not directly readable.

## Install

```bash
git clone git@github.com:PolybiusPro/MSI-P13-Display.git
cd MSI-P13-Display
bash scripts/install.sh
```

This installs native system packages, loads `vkms`, the udev rule, and a systemd user service that starts the panel monitor at graphical login.

```bash
systemctl --user status msi-p13-panel-monitor.service
journalctl --user -u msi-p13-panel-monitor.service -f
tail -f ~/.local/state/msi-p13-display/panel-monitor.log
```

Remove the service:

```bash
bash scripts/install.sh --remove
```

## Usage

The USB panel gets a vkms DRM output (for example `Virtual-1`) in Display Settings.

Send a still image or animation:

```bash
PYTHONPATH=src python3 -m msi_p13_display.send_image photo.jpg
PYTHONPATH=src python3 -m msi_p13_display.send_image animation.gif
```

Run the panel monitor manually:

```bash
PYTHONPATH=src python3 -m msi_p13_display.panel_monitor --shell
```

If Virtual-1 has stale resolutions from earlier runs:

```bash
sudo bash scripts/reset-vkms-modes.sh
```

## Stable Frame Settings

```text
JPEG quality       60
Pillow subsampling 2
USB chunk size     4096
```

## Layout

```text
src/msi_p13_display/  driver, vkms setup, panel_monitor.py, send_image.py
docs/en/              protocol guide
scripts/              install.sh, udev rule, driver wrapper
```

## Documentation

- [Protocol guide](docs/en/protocol.md)
