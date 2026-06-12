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
- KDE Plasma Wayland for the panel monitor (`krfb`, `python3-dbus`)

## Install

```bash
git clone git@github.com:PolybiusPro/MSI-P13-Display.git
cd MSI-P13-Display
bash scripts/install.sh
```

This installs system packages, a Python venv, the udev rule, and a systemd user
service that starts the panel monitor at graphical login.

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

The USB panel appears as `Virtual-MSI-P13` in Display Settings.

Send a still image or animation:

```bash
source .venv/bin/activate
python examples/send_image.py photo.jpg
python examples/send_image.py animation.gif
```

Run the panel monitor manually:

```bash
python examples/panel_monitor.py --shell
```

## Stable Frame Settings

```text
JPEG quality       60
Pillow subsampling 2
USB chunk size     4096
```

## Layout

```text
src/msi_p13_display/  display driver, capture, streaming
examples/             panel_monitor.py, send_image.py
docs/en/              protocol guide
scripts/              install.sh, udev rule, driver wrapper
```

## Documentation

- [Protocol guide](docs/en/protocol.md)
