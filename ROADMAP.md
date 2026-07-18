# fan-control roadmap

## Shipped

- Manual, Silent, Balanced, Performance, custom-curve, and EC-auto modes
- Linked or independent fan targets with accurate cap-relative percentages
- Validated, sorted, deduplicated custom fan curves
- Curve hysteresis and a cap-independent critical-temperature override
- Automatic firmware handoff when temperature data becomes unavailable
- Serialized ioctl access and clean daemon/dashboard ownership handoff
- Persistent configuration shared through `/etc/fan-control.json`
- Live hwmon sensors plus NVIDIA temperature fallback through `nvidia-smi`
- 30-minute CPU temperature, GPU temperature, and fan-duty history
- Responsive dark/light dashboard, browser alerts, and demo mode
- Local-origin API protection, request limits, and security headers

## Next

- Fan RPM once a verified read-only tach source exists for this hardware
- Optional per-GPU fan curves where the EC exposes independent control safely
- Import/export for named custom curves
- Packaging for common Linux distributions

## Later

- System tray status and quick profile switching
- Small `fan-ctl` command-line client
- Optional desktop notifications outside the browser

RPM remains intentionally unimplemented: the known Uniwill value on the target
GWTN156-2BK is duty, not tach speed, and probing unknown EC registers risks
hardware state. It should only be added after a read-only register is verified.
