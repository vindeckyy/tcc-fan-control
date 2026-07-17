#!/usr/bin/env python3
"""Fan control daemon for Tongfang/Clevo barebones using /dev/tuxedo_io.

Temperature source: k10temp (or any hwmon).
Curve: list of (temp_c, pwm 0-198). Linear interp.
"""

import argparse, ctypes, fcntl, json, os, pathlib, signal, sys, time

MAGIC_RD = 0xEF; MAGIC_WR = 0xF0
IOC_R, IOC_W, SZ = 2, 1, 8
def ioc(d,t,n,s): return (d<<30)|(t<<8)|(n<<0)|(s<<16)

R_FS1  = ioc(IOC_R, MAGIC_RD, 0x10, SZ)
R_FS2  = ioc(IOC_R, MAGIC_RD, 0x11, SZ)
R_TEMP = ioc(IOC_R, MAGIC_RD, 0x12, SZ)
W_FS1  = ioc(IOC_W, MAGIC_WR, 0x10, SZ)
W_FS2  = ioc(IOC_W, MAGIC_WR, 0x11, SZ)
W_AUTO = ioc(0,        MAGIC_WR, 0x14, 0)

FD = os.open('/dev/tuxedo_io', os.O_RDWR)
BUF = (ctypes.c_int64)()

def rd(cmd):
    BUF.value = 0
    fcntl.ioctl(FD, cmd, BUF, True)
    return BUF.value & 0xFF

def wr(cmd, v):
    BUF.value = int(v)
    fcntl.ioctl(FD, cmd, BUF, True)

DEFAULT_CURVE = [
    (0,0),(50,0),(60,50),(70,100),(80,160),(85,200),(90,230),(95,198),(110,198),
]

def find_k10():
    for h in sorted(pathlib.Path('/sys/class/hwmon').glob('hwmon*')):
        if (h/'name').read_text().strip() == 'k10temp':
            return h
    return None

def cpu_temp(hw):
    if not hw: return None
    return int((hw/'temp1_input').read_text().strip())/1000

def interp(t, curve):
    if t <= curve[0][0]: return curve[0][1]
    if t >= curve[-1][0]: return curve[-1][1]
    for (t0,p0),(t1,p1) in zip(curve, curve[1:]):
        if t0 <= t < t1:
            return p0 + (p1-p0)*(t-t0)/(t1-t0) if t1>t0 else p0

def loop(args):
    hw = find_k10()
    curve = args.curve
    stop = [False]
    signal.signal(signal.SIGTERM, lambda *_: stop.__setitem__(0, True))
    signal.signal(signal.SIGINT,  lambda *_: stop.__setitem__(0, True))
    print(f"fan daemon: {len(curve)} curve pts, interval={args.interval}s")
    while not stop[0]:
        t = cpu_temp(hw)
        if t is None: t = float(rd(R_TEMP))
        if t is None: continue
        pwm = int(round(interp(t, curve)))
        for f, cmd in [(1, W_FS1), (2, W_FS2)]:
            if args.fans >= f and abs(rd(cmd) - pwm) >= args.hysteresis:
                wr(cmd, pwm)
        time.sleep(args.interval)
    try: fcntl.ioctl(FD, W_AUTO)
    except Exception: pass
    os.close(FD)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--interval', type=float, default=2.0)
    ap.add_argument('--curve', type=json.loads, default=None)
    ap.add_argument('--fans', type=int, default=2)
    ap.add_argument('--hysteresis', type=int, default=5)
    args = ap.parse_args()
    if args.curve is None: args.curve = DEFAULT_CURVE
    try:
        loop(args)
    except PermissionError:
        sys.exit('need root')
    except FileNotFoundError:
        sys.exit('/dev/tuxedo_io not found — load tuxedo-drivers')

if __name__ == '__main__':
    main()