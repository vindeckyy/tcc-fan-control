# fan-control

Fan control for Tongfang/Clevo barebones using the kernel's `/dev/tuxedo_io`
ioctl interface. It includes a temperature-driven daemon and a local web
dashboard at `http://127.0.0.1:4444`.

## Highlights

- Manual, Silent, Balanced, Performance, custom-curve, and firmware-auto modes
- Independent or linked CPU/GPU fan control
- Safe duty clamping plus an independent critical-temperature override
- Curve hysteresis to prevent rapid fan hunting
- Live hwmon sensors, NVIDIA temperatures via `nvidia-smi`, and 30 minutes of CPU/GPU/duty history
- Persistent settings shared by the daemon and dashboard
- Responsive dark/light UI, browser temperature alerts, and a hardware-free demo
- Serialized EC access and clean handoff between the daemon and GUI

## Install

```bash
sudo cp fan-daemon.py fan-gui.py /usr/local/bin/
sudo chmod +x /usr/local/bin/fan-daemon /usr/local/bin/fan-gui
sudo cp fan-daemon.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fan-daemon
```

The kernel's `tuxedo-io` driver must be loaded. The EC firmware becomes
unstable near duty 200, so all normal writes are capped at 198.

## Run

```bash
sudo fan-gui
sudo fan-daemon --profile balanced
```

Preview the full dashboard without root or supported hardware:

```bash
FAN_CONTROL_CONFIG=/tmp/fan-control-demo.json fan-gui --demo
```

Preview daemon decisions without opening the EC device:

```bash
fan-daemon --dry-run --profile silent
```

## Configuration

Both programs read `/etc/fan-control.json`. The dashboard writes it
atomically when settings change. Supported keys are `profile`, `curve`,
`max_duty`, `hysteresis`, and `critical_temp`.

```json
{
  "profile": "balanced",
  "max_duty": 198,
  "hysteresis": 5,
  "critical_temp": 95
}
```

At or above `critical_temp`, fan-control requests duty 198 even when the user
has selected a lower noise cap. After three invalid temperature reads, the
daemon returns control to the EC firmware until a valid sensor reading returns.

## Checks

```bash
python3 -m unittest -v
python3 -m py_compile fan-daemon.py fan-gui.py
```
