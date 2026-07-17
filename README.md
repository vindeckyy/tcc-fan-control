# tcc-fan-control

Fan control for Tongfang/Clevo laptops running TUXEDO Control Center's `tuxedo-drivers`.

Two ways to use it:
- `tcc-fan-control.py` — daemon that polls CPU temp and writes fan duty via a temperature curve.
- `tcc-fan-gui.py` — web UI on `http://127.0.0.1:4444` with sliders, presets, live sensors, and 500ms re-assert.

## Requirements

- `tuxedo-drivers` kernel module loaded (provides `/dev/tuxedo_io`)
- `python3` with `tkinter` (for the daemon — `apt install python3-tk` if needed)
- Root access (the ioctl path needs it)

## Setup

```bash
sudo cp tcc-fan-control.py /usr/local/bin/tcc-fan-control.py
sudo cp tcc-fan-gui.py /usr/local/bin/tcc-fan-gui
sudo chmod +x /usr/local/bin/tcc-fan-*
```

## Daemon

```bash
sudo systemctl enable --now tcc-fan.service
```

Custom curve via JSON:

```bash
sudo tcc-fan-control.py --curve '[[0,0],[60,50],[80,160],[95,255]]'
```

## GUI

```bash
sudo tcc-fan-gui
```

Opens `http://127.0.0.1:4444` in your browser. Drag the sliders; close the window to release control back to the EC.

## How it works

The kernel exposes `/dev/tuxedo_io` with ioctl magic `0xEC`. `tuxedo_io_ioctl.h` defines the Uniwill/Clevo fan read/write commands. Both `tccd` (TUXEDO's daemon) and this project use the same ioctl path:

- `R_UW_FANSPEED` / `R_UW_FANSPEED2` — read current duty (0–255)
- `W_UW_FANSPEED` / `W_UW_FANSPEED2` — write duty
- `R_UW_FAN_TEMP` / `R_UW_FAN_TEMP2` — read EC internal temp sensors

Note: on x86_64, the `_IOR`/`_IOW` macros use `sizeof(pointer) = 8` for the size field. Get this wrong and the ioctl silently returns 0.

## Caveats

- Requires DMI-matching in `tuxedo-drivers` for hardware detection. Some Tongfang barebones (e.g. GWTN156-2BK) don't match any known table — fan control still works, but profile-specific DMI features (TDP limits, profile modes) won't.
- Setting PWM above ~200 wraps on this EC. Both tools cap at 200 (≈80% slider).
- If TCC is running, stop it first: `sudo systemctl stop tccd`.