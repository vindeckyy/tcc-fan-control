#!/usr/bin/env python3
"""
TCC-style fan control daemon for Tongfang/Clevo laptops.
Uses /dev/tuxedo_io (ioctl magic 0xEC) to read/write EC fan registers.
Temperature from k10temp (Ryzen CPU).

Matches TCC's actual ioctl interface: sizes are 8 bytes (pointer on x86_64).
Registers: 0x1804 (fan1 duty), 0x1805 (fan2 duty) via Uniwill direct_fan_control.
Mode register 0x0751 bit 0x40 = manual fan control enable.

curve: list of (temp_celsius, pwm_value) — linear interpolation between points.
"""

import argparse
import ctypes
import fcntl
import json
import os
import pathlib
import signal
import sys
import time

# ── ioctl from strace of real tccd: sizes are sizeof(pointer) = 8 on x86_64 ──
MAGIC_IOCTL = 0xEC
MAGIC_READ_UW = 0xEF
MAGIC_WRITE_UW = 0xF0

IOC_NONE, IOC_WRITE, IOC_READ = 0, 1, 2
SZ = 8  # sizeof(int32_t*) on x86_64

def _IOC(d, t, n, s): return (d << 30) | (t << 8) | (n << 0) | (s << 16)

R_UW_FANSPEED   = _IOC(IOC_READ,  MAGIC_READ_UW,  0x10, SZ)
R_UW_FANSPEED2  = _IOC(IOC_READ,  MAGIC_READ_UW,  0x11, SZ)
R_UW_FAN_TEMP   = _IOC(IOC_READ,  MAGIC_READ_UW,  0x12, SZ)
R_UW_FAN_TEMP2  = _IOC(IOC_READ,  MAGIC_READ_UW,  0x13, SZ)
R_UW_MODE       = _IOC(IOC_READ,  MAGIC_READ_UW,  0x14, SZ)
R_UW_FANS_MIN_SPEED = _IOC(IOC_READ, MAGIC_READ_UW, 0x17, SZ)

W_UW_FANSPEED  = _IOC(IOC_WRITE, MAGIC_WRITE_UW, 0x10, SZ)
W_UW_FANSPEED2 = _IOC(IOC_WRITE, MAGIC_WRITE_UW, 0x11, SZ)
W_UW_FANAUTO   = _IOC(IOC_NONE,  MAGIC_WRITE_UW, 0x14, 0)

# ── Default curve (0-255 PWM) ────────────────────────────────────────────
# TCC quiet profile: silent below 60°C, ramping 60-85°C, max at 95°C
DEFAULT_CURVE = [
    (0,   0),
    (50,  0),
    (60,  50),
    (70,  100),
    (80,  160),
    (85,  200),
    (90,  230),
    (95,  255),
    (110, 255),
]

# ── Temperature sources ──────────────────────────────────────────────────
def find_k10temp_hwmon():
    for hwmon in sorted(pathlib.Path("/sys/class/hwmon").glob("hwmon*")):
        name = (hwmon / "name").read_text().strip()
        if name == "k10temp":
            return hwmon
    return None

def read_cpu_temp(hwmon_base):
    tpath = hwmon_base / "temp1_input"
    if not tpath.exists():
        return None
    raw = tpath.read_text().strip()
    return int(raw) / 1000.0

# ── Fan control ──────────────────────────────────────────────────────────
class TuxedoFanControl:
    def __init__(self):
        self.fd = os.open("/dev/tuxedo_io", os.O_RDWR)
        self.buf = (ctypes.c_int64)()  # always 8 bytes for ptr-sized ioctl
        self.last_pwm = [0, 0]

    def read_fan(self, fan=1):
        cmd = R_UW_FANSPEED if fan == 1 else R_UW_FANSPEED2
        fcntl.ioctl(self.fd, cmd, self.buf, True)
        return self.buf.value

    def set_fan(self, fan, pwm_value):
        pwm = max(0, min(255, int(pwm_value)))
        self.buf.value = pwm
        cmd = W_UW_FANSPEED if fan == 1 else W_UW_FANSPEED2
        fcntl.ioctl(self.fd, cmd, self.buf, True)
        self.last_pwm[fan - 1] = pwm

    def read_ec_temp(self, sensor=1):
        cmd = R_UW_FAN_TEMP if sensor == 1 else R_UW_FAN_TEMP2
        fcntl.ioctl(self.fd, cmd, self.buf, True)
        return self.buf.value

    def read_mode(self):
        fcntl.ioctl(self.fd, R_UW_MODE, self.buf, True)
        return self.buf.value

    def set_auto(self):
        fcntl.ioctl(self.fd, W_UW_FANAUTO)

    def close(self):
        self.set_auto()
        os.close(self.fd)


# ── Curve interpolation ──────────────────────────────────────────────────
def interpolate_pwm(temp_c, curve):
    if temp_c <= curve[0][0]:
        return curve[0][1]
    if temp_c >= curve[-1][0]:
        return curve[-1][1]
    for i in range(len(curve) - 1):
        t0, p0 = curve[i]
        t1, p1 = curve[i + 1]
        if t0 <= temp_c < t1:
            frac = (temp_c - t0) / (t1 - t0)
            return p0 + (p1 - p0) * frac
    return curve[-1][1]


# ── Daemon ───────────────────────────────────────────────────────────────
def daemon_loop(args):
    fc = TuxedoFanControl()
    hwmon = find_k10temp_hwmon()

    if not hwmon:
        print("Warning: k10temp not found, using EC temp sensor as fallback")

    curve = args.curve
    fan_count = args.fans
    stop = False

    def handle_signal(sig, frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    # Read initial state
    ec_temp = fc.read_ec_temp(1)
    mode = fc.read_mode()
    print(f"Initial state — EC temp: {ec_temp}°C,  Mode reg: 0x{mode:02x},  Fan1: {fc.read_fan(1)},  Fan2: {fc.read_fan(2)}")
    print(f"Controlling {fan_count} fan(s), interval={args.interval}s, curve points: {len(curve)}")

    while not stop:
        # Read CPU temperature
        temp = read_cpu_temp(hwmon) if hwmon else None
        if temp is None:
            ec_temp = fc.read_ec_temp(1)
            temp = ec_temp

        pwm = int(round(interpolate_pwm(temp, curve)))

        for f in range(1, fan_count + 1):
            current = fc.read_fan(f)
            if abs(pwm - current) >= args.hysteresis or pwm == 0:
                fc.set_fan(f, pwm)

        if args.verbose:
            t1 = fc.read_ec_temp(1)
            t2 = fc.read_ec_temp(2) if fan_count > 1 else 0
            f1 = fc.read_fan(1)
            f2 = fc.read_fan(2) if fan_count > 1 else 0
            print(f"CPU:{temp:.1f}°C  EC:{t1}/{t2}  PWM:{pwm}  f1:{f1}  f2:{f2}")

        time.sleep(args.interval)

    print("Restoring automatic fan control...")
    fc.close()
    print("Stopped.")


def main():
    ap = argparse.ArgumentParser(description="TCC-style fan control daemon")
    ap.add_argument("--interval", type=float, default=2.0,
                    help="Poll interval in seconds (default 2.0)")
    ap.add_argument("--curve", type=json.loads, default=None,
                    help='Fan curve as JSON list of [temp, pwm] pairs. '
                         'e.g. \'[[0,0],[50,0],[60,50],[70,100],[80,160],[85,200],[95,255]]\'')
    ap.add_argument("--fans", type=int, default=2,
                    help="Number of fans (default 2)")
    ap.add_argument("--hysteresis", type=int, default=5,
                    help="PWM change threshold (default 5)")
    ap.add_argument("--pidfile", default="/run/tuxedo-fan-control.pid",
                    help="PID file path")
    ap.add_argument("--daemon", action="store_true",
                    help="Fork into background")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="Print per-cycle state")
    args = ap.parse_args()

    if args.curve is None:
        args.curve = DEFAULT_CURVE

    if args.daemon:
        pid = os.fork()
        if pid > 0:
            with open(args.pidfile, "w") as f:
                f.write(str(pid))
            sys.exit(0)
        os.setsid()
        null = os.open(os.devnull, os.O_RDWR)
        os.dup2(null, 0)
        os.dup2(null, 1)
        os.dup2(null, 2)
        os.close(null)

    try:
        daemon_loop(args)
    except PermissionError:
        print("ERROR: Need root for /dev/tuxedo_io. Run with sudo.", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError:
        print("ERROR: /dev/tuxedo_io not found. Is tuxedo-drivers loaded?", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
