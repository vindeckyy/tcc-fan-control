#!/usr/bin/env python3
"""Temperature-driven fan control for Clevo/Tongfang barebones."""

import argparse
import ctypes
import fcntl
import json
import os
import pathlib
import signal
import sys
import time

MAGIC_RD, MAGIC_WR = 0xEF, 0xF0
IOC_R, IOC_W, SZ = 2, 1, 8
SAFE_MAX_DUTY = 198


def ioc(direction, kind, number, size):
    return (direction << 30) | (kind << 8) | number | (size << 16)


R_FS1 = ioc(IOC_R, MAGIC_RD, 0x10, SZ)
R_FS2 = ioc(IOC_R, MAGIC_RD, 0x11, SZ)
R_TEMP = ioc(IOC_R, MAGIC_RD, 0x12, SZ)
W_FS1 = ioc(IOC_W, MAGIC_WR, 0x10, SZ)
W_FS2 = ioc(IOC_W, MAGIC_WR, 0x11, SZ)
W_MODE = ioc(IOC_W, MAGIC_WR, 0x12, SZ)
W_AUTO = ioc(0, MAGIC_WR, 0x14, 0)

PROFILES = {
    "silent": [(0, 0), (50, 0), (60, 0), (70, 60), (80, 120), (90, 170), (95, 198), (110, 198)],
    "balanced": [(0, 0), (50, 0), (60, 50), (70, 100), (80, 150), (90, 180), (95, 198), (110, 198)],
    "performance": [(0, 0), (50, 0), (60, 80), (70, 140), (80, 180), (85, 198), (110, 198)],
}


class EC:
    def __init__(self, path):
        self.fd = os.open(path, os.O_RDWR)

    def read(self, command):
        buf = ctypes.c_int64()
        fcntl.ioctl(self.fd, command, buf, True)
        return buf.value & 0xFF

    def write(self, command, value):
        buf = ctypes.c_int64(int(value))
        fcntl.ioctl(self.fd, command, buf, True)

    def lock(self):
        self.write(W_MODE, 0x40)

    def release(self):
        fcntl.ioctl(self.fd, W_AUTO)

    def close(self):
        os.close(self.fd)


def find_cpu_sensor():
    for hwmon in sorted(pathlib.Path("/sys/class/hwmon").glob("hwmon*")):
        try:
            if (hwmon / "name").read_text().strip() == "k10temp":
                return hwmon / "temp1_input"
        except OSError:
            continue
    return None


def find_ec_device():
    configured = os.environ.get("FAN_CONTROL_DEVICE")
    if configured:
        return configured
    candidates = sorted(pathlib.Path("/dev").glob("*_io"))
    if len(candidates) == 1:
        return str(candidates[0])
    if not candidates:
        raise FileNotFoundError("Clevo/Tongfang fan-control device not found")
    raise FileNotFoundError("multiple fan-control devices found; set FAN_CONTROL_DEVICE")


def cpu_temp(sensor):
    try:
        return int(sensor.read_text().strip()) / 1000 if sensor else None
    except (OSError, ValueError):
        return None


def normalize_curve(curve):
    """Validate, sort, deduplicate, and clamp a user-provided curve."""
    if not isinstance(curve, list):
        raise ValueError("curve must be a JSON list")
    points = {}
    for row in curve:
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            raise ValueError("each curve point must be [temperature, duty]")
        temp, duty = row
        if isinstance(temp, bool) or isinstance(duty, bool) or not isinstance(temp, (int, float)) or not isinstance(duty, (int, float)):
            raise ValueError("curve values must be numbers")
        points[max(0, min(150, float(temp)))] = max(0, min(SAFE_MAX_DUTY, int(round(duty))))
    curve = sorted(points.items())
    if len(curve) < 2:
        raise ValueError("curve needs at least two unique temperatures")
    return curve


def interpolate(temp, curve):
    if temp <= curve[0][0]:
        return curve[0][1]
    if temp >= curve[-1][0]:
        return curve[-1][1]
    for (t0, d0), (t1, d1) in zip(curve, curve[1:]):
        if t0 <= temp < t1:
            return d0 + (d1 - d0) * (temp - t0) / (t1 - t0)
    raise ValueError("invalid fan curve")


def target_duty(temp, curve, max_duty=SAFE_MAX_DUTY, critical_temp=95):
    """Critical cooling deliberately bypasses the user noise cap."""
    if temp >= critical_temp:
        return SAFE_MAX_DUTY
    return max(0, min(max_duty, int(round(interpolate(temp, curve)))))


def load_config(path):
    if not path:
        return {}
    try:
        value = json.loads(pathlib.Path(path).read_text())
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read config: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("config must contain a JSON object")
    return value


def run(args):
    config = load_config(args.config)
    profile = config.get("profile", args.profile)
    if profile not in (*PROFILES, "custom"):
        profile = args.profile
    curve = normalize_curve(args.curve or (config.get("curve") if profile == "custom" else None) or PROFILES.get(profile, PROFILES[args.profile]))
    max_duty = max(0, min(SAFE_MAX_DUTY, int(config.get("max_duty", args.max_duty))))
    hysteresis = max(0, int(config.get("hysteresis", args.hysteresis)))
    critical_temp = max(70, min(110, float(config.get("critical_temp", args.critical_temp))))
    sensor = find_cpu_sensor()
    ec = None if args.dry_run else EC(args.device or find_ec_device())
    stopped = False

    def stop(*_):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    if ec:
        ec.lock()
    print(f"fan daemon: profile={profile}, {len(curve)} points, interval={args.interval}s, cap={max_duty}")

    missing = 0
    released_for_fault = False
    try:
        while not stopped:
            temp = cpu_temp(sensor)
            if temp is None and ec:
                try:
                    temp = float(ec.read(R_TEMP))
                except OSError:
                    temp = None
            if temp is None or not 0 < temp <= 150:
                missing += 1
                if ec and missing >= 3 and not released_for_fault:
                    print("temperature unavailable; returning control to the EC", file=sys.stderr)
                    ec.release()
                    released_for_fault = True
                time.sleep(args.interval)
                continue

            if released_for_fault and ec:
                ec.lock()
                released_for_fault = False
            missing = 0
            duty = target_duty(temp, curve, max_duty, critical_temp)
            if args.dry_run:
                print(f"{temp:.1f}°C -> duty {duty}")
            else:
                for fan, read_cmd, write_cmd in ((1, R_FS1, W_FS1), (2, R_FS2, W_FS2)):
                    if fan <= args.fans and (temp >= critical_temp or abs(ec.read(read_cmd) - duty) >= hysteresis):
                        ec.write(write_cmd, duty)
            time.sleep(args.interval)
    finally:
        if ec:
            try:
                ec.release()
            except OSError:
                pass
            ec.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--profile", choices=PROFILES, default="balanced")
    parser.add_argument("--curve", type=json.loads, help="JSON list of [temperature, duty] points")
    parser.add_argument("--config", default="/etc/fan-control.json")
    parser.add_argument("--device", help="kernel device path (or set FAN_CONTROL_DEVICE)")
    parser.add_argument("--fans", type=int, choices=(1, 2), default=2)
    parser.add_argument("--hysteresis", type=int, default=5)
    parser.add_argument("--max-duty", type=int, default=SAFE_MAX_DUTY)
    parser.add_argument("--critical-temp", type=float, default=95)
    parser.add_argument("--dry-run", action="store_true", help="print decisions without opening the EC device")
    args = parser.parse_args()
    if args.interval <= 0 or args.hysteresis < 0 or not 70 <= args.critical_temp <= 110:
        parser.error("interval must be positive, hysteresis non-negative, and critical temperature 70-110")
    try:
        run(args)
    except PermissionError:
        sys.exit("need root")
    except FileNotFoundError as exc:
        sys.exit(str(exc))
    except (OSError, ValueError) as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()
