# fan-control

Fan control for Tongfang/Clevo barebones using `/dev/tuxedo_io`.

Two interfaces:
- `fan-daemon.py` — temperature-driven curve. Polls k10temp, writes duty via ioctl.
- `fan-gui.py` — web UI on `http://127.0.0.1:4444` with sliders, presets, live sensors.

## Install

```bash
sudo cp fan-daemon.py fan-gui.py /usr/local/bin/
sudo chmod +x /usr/local/bin/fan-daemon /usr/local/bin/fan-gui
sudo cp fan-daemon.service /etc/systemd/system/
sudo systemctl enable --now fan-daemon
```

## Run

```bash
sudo fan-daemon                 # start the curve-based daemon
sudo fan-gui                    # open the web UI
```

## How it works

`/dev/tuxedo_io` exposes ioctl commands (magic `0xEC`) for reading and writing
EC fan duty registers. Both the kernel-supplied `tuxedo-drivers` userspace
companion and this project use the same path:

- Read duty: `R_FS1`, `R_FS2`
- Write duty: `W_FS1`, `W_FS2`
- Restore auto: `W_AUTO` (ioctl with no payload)
- Lock manual mode: write `0x40` to `W_MODE` so the EC firmware doesn't
  fight your duty values with its own auto-curve.

Note: on x86_64, the `_IOR`/`_IOW` macros use `sizeof(pointer) = 8` for the
size field. Get this wrong and the ioctl silently returns 0.

## Caveats

- Requires `tuxedo-drivers` kernel module loaded.
- Cap duty at 198 — the EC firmware wraps/oscillates at the top of its 0–200 range.
- Set bit `0x40` in mode register before writing, otherwise the EC reverts
  to its auto-curve within ~100ms.