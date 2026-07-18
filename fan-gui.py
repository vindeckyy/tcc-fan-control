#!/usr/bin/env python3
"""Local web dashboard for Clevo/Tongfang fan control."""

import argparse
import collections
import ctypes
import fcntl
import http.server
import json
import math
import os
import pathlib
import signal
import subprocess
import sys
import threading
import time
import webbrowser

MAGIC_RD, MAGIC_WR = 0xEF, 0xF0
IOC_R, IOC_W, SZ = 2, 1, 8
SAFE_MAX_DUTY = 198
CONFIG_PATH = pathlib.Path(os.environ.get("FAN_CONTROL_CONFIG", "/etc/fan-control.json"))


def ioc(direction, kind, number, size):
    return (direction << 30) | (kind << 8) | number | (size << 16)


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


R_FS1 = ioc(IOC_R, MAGIC_RD, 0x10, SZ)
R_FS2 = ioc(IOC_R, MAGIC_RD, 0x11, SZ)
R_TEMP = ioc(IOC_R, MAGIC_RD, 0x12, SZ)
R_TEMP2 = ioc(IOC_R, MAGIC_RD, 0x13, SZ)
W_FS1 = ioc(IOC_W, MAGIC_WR, 0x10, SZ)
W_FS2 = ioc(IOC_W, MAGIC_WR, 0x11, SZ)
W_MODE = ioc(IOC_W, MAGIC_WR, 0x12, SZ)
W_AUTO = ioc(0, MAGIC_WR, 0x14, 0)

PROFILES = {
    "silent": [(0, 0), (50, 0), (60, 0), (70, 60), (80, 120), (90, 170), (95, 198), (110, 198)],
    "balanced": [(0, 0), (50, 0), (60, 50), (70, 100), (80, 150), (90, 180), (95, 198), (110, 198)],
    "performance": [(0, 0), (50, 0), (60, 80), (70, 140), (80, 180), (85, 198), (110, 198)],
    "custom": [(0, 0), (55, 0), (65, 55), (75, 110), (85, 165), (95, 198), (110, 198)],
}

FD = None
EC_LOCK = threading.RLock()
STATE_LOCK = threading.RLock()
STOP = threading.Event()
HISTORY = collections.deque(maxlen=900)  # 30 minutes at a 2s cadence
DEMO = False
_demo = {"fan1": 0, "fan2": 0}


def rd(command):
    if DEMO:
        if command == R_FS1:
            return _demo["fan1"]
        if command == R_FS2:
            return _demo["fan2"]
        return round(62 + 10 * math.sin(time.monotonic() / 18)) + (2 if command == R_TEMP2 else 0)
    with EC_LOCK:
        buf = ctypes.c_int64()
        fcntl.ioctl(FD, command, buf, True)
        return buf.value & 0xFF


def wr(command, value):
    if DEMO:
        if command == W_FS1:
            _demo["fan1"] = int(value)
        elif command == W_FS2:
            _demo["fan2"] = int(value)
        return
    with EC_LOCK:
        buf = ctypes.c_int64(int(value))
        fcntl.ioctl(FD, command, buf, True)


def lock_manual():
    wr(W_MODE, 0x40)


def release_manual():
    if DEMO:
        return
    with EC_LOCK:
        fcntl.ioctl(FD, W_AUTO)


def write_duty(fan, duty):
    wr((W_FS1, W_FS2)[fan - 1], max(0, min(SAFE_MAX_DUTY, int(duty))))


def normalize_curve(curve):
    if not isinstance(curve, list):
        raise ValueError("curve must be a list")
    points = {}
    for row in curve:
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            raise ValueError("each point must be [temperature, duty]")
        temp, duty = row
        if isinstance(temp, bool) or isinstance(duty, bool) or not isinstance(temp, (int, float)) or not isinstance(duty, (int, float)):
            raise ValueError("curve values must be numbers")
        points[max(0, min(150, int(temp)))] = max(0, min(SAFE_MAX_DUTY, int(duty)))
    result = sorted(points.items())
    if len(result) < 2:
        raise ValueError("curve needs at least two unique temperatures")
    return result


def interpolate(temp, curve):
    if temp <= curve[0][0]:
        return curve[0][1]
    if temp >= curve[-1][0]:
        return curve[-1][1]
    for (t0, d0), (t1, d1) in zip(curve, curve[1:]):
        if t0 <= temp < t1:
            return d0 + (d1 - d0) * (temp - t0) / (t1 - t0)
    return curve[-1][1]


def load_config():
    try:
        data = json.loads(CONFIG_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


saved = load_config()
try:
    saved_curve = normalize_curve(saved.get("curve", PROFILES["custom"]))
except ValueError:
    saved_curve = list(PROFILES["custom"])

state = {
    "mode": saved.get("mode", "manual") if saved.get("mode") in ("manual", "curve", "released") else "manual",
    "profile": saved.get("profile", "balanced") if saved.get("profile") in PROFILES else "balanced",
    "targets": {1: 0, 2: 0},
    "max_duty": max(20, min(SAFE_MAX_DUTY, int(saved.get("max_duty", SAFE_MAX_DUTY)))),
    "hysteresis": max(0, min(30, int(saved.get("hysteresis", 5)))),
    "critical_temp": max(70, min(110, int(saved.get("critical_temp", 95)))),
    "custom_curve": saved_curve,
}


def save_config():
    with STATE_LOCK:
        payload = {
            "mode": state["mode"],
            "profile": state["profile"],
            "max_duty": state["max_duty"],
            "hysteresis": state["hysteresis"],
            "critical_temp": state["critical_temp"],
            "curve": state["custom_curve"],
        }
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = CONFIG_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n")
        temporary.replace(CONFIG_PATH)
    except OSError as exc:
        print(f"warning: could not save {CONFIG_PATH}: {exc}", file=sys.stderr)


_sensor_cache = []
_readback = {"fan1": 0, "fan2": 0, "ec_temp1": 0, "ec_temp2": 0, "updated": 0}
_last_curve_duty = None


def parse_nvidia_smi(output):
    sensors = []
    for line in output.splitlines():
        parts = [part.strip() for part in line.split(",", 2)]
        if len(parts) != 3:
            continue
        index, temperature, name = parts
        try:
            temperature = float(temperature)
        except ValueError:
            continue
        if 0 <= temperature <= 150:
            sensors.append({"name": "nvidia", "label": f"GPU {index} · {name}", "temp": temperature})
    return sensors


def nvidia_sensors():
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,temperature.gpu,name", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=2, check=False,
        )
        return parse_nvidia_smi(result.stdout) if result.returncode == 0 else []
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return []


def sensor_loop():
    global _sensor_cache
    while not STOP.is_set():
        found = ([{"name": "k10temp", "label": "Tctl", "temp": 62 + 10 * math.sin(time.monotonic() / 18)},
                  {"name": "amdgpu", "label": "edge", "temp": 54 + 7 * math.sin(time.monotonic() / 23)}]
                 if DEMO else [])
        if DEMO:
            with STATE_LOCK:
                _sensor_cache = found
            STOP.wait(1)
            continue
        for hwmon in sorted(pathlib.Path("/sys/class/hwmon").glob("hwmon*")):
            try:
                name = (hwmon / "name").read_text().strip()
            except OSError:
                continue
            for path in sorted(hwmon.glob("temp*_input")):
                try:
                    temp = int(path.read_text().strip()) / 1000
                    if 0 <= temp <= 150:
                        label_path = path.with_name(path.name.replace("_input", "_label"))
                        label = label_path.read_text().strip() if label_path.exists() else path.stem.replace("_input", "")
                        found.append({"name": name, "label": label, "temp": temp})
                except (OSError, ValueError):
                    continue
        if not any("nvidia" in sensor["name"].lower() for sensor in found):
            found.extend(nvidia_sensors())
        with STATE_LOCK:
            _sensor_cache = found
        STOP.wait(2)


def readback_loop():
    global _readback
    while not STOP.is_set():
        try:
            snap = {"fan1": rd(R_FS1), "fan2": rd(R_FS2), "ec_temp1": rd(R_TEMP), "ec_temp2": rd(R_TEMP2), "updated": time.time()}
            with STATE_LOCK:
                _readback = snap
        except OSError as exc:
            print(f"EC read failed: {exc}", file=sys.stderr)
        STOP.wait(0.25)


def primary_temp():
    with STATE_LOCK:
        sensors = list(_sensor_cache)
        fallback = _readback["ec_temp1"]
    k10 = [s["temp"] for s in sensors if s["name"] == "k10temp"]
    return max(k10) if k10 else fallback if 0 < fallback <= 150 else None


def gpu_temp():
    with STATE_LOCK:
        values = [sensor["temp"] for sensor in _sensor_cache
                  if "nvidia" in sensor["name"].lower() or "amdgpu" in sensor["name"].lower()]
    return max(values) if values else None


def control_loop():
    global _last_curve_duty
    missing_temperature = 0
    released_for_fault = False
    while not STOP.is_set():
        with STATE_LOCK:
            mode = state["mode"]
            profile = state["profile"]
            targets = dict(state["targets"])
            cap = state["max_duty"]
            hysteresis = state["hysteresis"]
            critical = state["critical_temp"]
            curve = list(state["custom_curve"] if profile == "custom" else PROFILES[profile])
            current = (_readback["fan1"], _readback["fan2"])
        temp = primary_temp()
        if mode == "released":
            missing_temperature = 0
            released_for_fault = False
            STOP.wait(0.25)
            continue
        if mode == "curve" and temp is None:
            missing_temperature += 1
            if missing_temperature >= 3 and not released_for_fault:
                try:
                    release_manual()
                    released_for_fault = True
                except OSError as exc:
                    print(f"EC release failed: {exc}", file=sys.stderr)
            STOP.wait(0.25)
            continue
        if released_for_fault:
            try:
                lock_manual()
            except OSError as exc:
                print(f"EC lock failed: {exc}", file=sys.stderr)
            released_for_fault = False
        missing_temperature = 0
        if temp is not None and temp >= critical:
            desired = (SAFE_MAX_DUTY, SAFE_MAX_DUTY)
        elif mode == "curve" and temp is not None:
            candidate = max(0, min(cap, int(round(interpolate(temp, curve)))))
            if _last_curve_duty is None or abs(candidate - _last_curve_duty) >= hysteresis:
                _last_curve_duty = candidate
            desired = (_last_curve_duty, _last_curve_duty)
        elif mode == "manual":
            desired = tuple(round(targets[fan] * cap / 100) for fan in (1, 2))
        else:
            STOP.wait(0.25)
            continue
        for fan, duty in enumerate(desired, 1):
            if temp is not None and temp >= critical or abs(current[fan - 1] - duty) >= max(2, hysteresis):
                try:
                    write_duty(fan, duty)
                except OSError as exc:
                    print(f"EC write failed: {exc}", file=sys.stderr)
        STOP.wait(0.25)


def history_loop():
    while not STOP.is_set():
        temp = primary_temp()
        with STATE_LOCK:
            HISTORY.append({"time": int(time.time()), "temp": temp, "gpu_temp": gpu_temp(), "fan1": _readback["fan1"], "fan2": _readback["fan2"]})
        STOP.wait(2)


def snapshot():
    with STATE_LOCK:
        result = dict(_readback)
        result.update({
            "targets": dict(state["targets"]), "mode": state["mode"], "profile": state["profile"],
            "max_duty": state["max_duty"], "hysteresis": state["hysteresis"],
            "critical_temp": state["critical_temp"], "custom_curve": list(state["custom_curve"]),
            "temps": list(_sensor_cache), "primary_temp": primary_temp(), "gpu_temp": gpu_temp(),
        })
    return result


HTML = r'''<!doctype html>
<html lang="en" data-theme="dark"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark light"><title>Fan Control</title>
<style>
*{box-sizing:border-box}[hidden]{display:none!important} :root{--bg:#090d14;--panel:#111823;--raised:#182231;--line:#263449;--text:#edf4ff;--muted:#91a0b7;--blue:#62a8ff;--cyan:#57dfcf;--amber:#ffbd5b;--red:#ff657a;--shadow:0 20px 60px #0005;color-scheme:dark}
[data-theme=light]{--bg:#eef3f8;--panel:#fff;--raised:#f4f7fb;--line:#d8e0eb;--text:#152033;--muted:#607089;--blue:#176ed1;--cyan:#008f82;--amber:#a96400;--red:#d42d47;--shadow:0 18px 50px #30405a18;color-scheme:light}
body{margin:0;background:radial-gradient(circle at 15% -10%,#173b6840,transparent 34%),var(--bg);color:var(--text);font:14px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,sans-serif;min-height:100vh}
button,input,select{font:inherit} button{color:inherit}.shell{width:min(1180px,calc(100% - 32px));margin:auto;padding:28px 0 48px}
header{display:flex;align-items:center;justify-content:space-between;gap:20px;margin-bottom:22px}.brand{display:flex;align-items:center;gap:13px}.logo{width:42px;height:42px;border-radius:13px;display:grid;place-items:center;background:linear-gradient(145deg,var(--blue),var(--cyan));color:#07111e;font-size:22px;box-shadow:0 8px 26px #399aff40}.brand h1{font-size:20px;margin:0}.brand p{color:var(--muted);margin:0;font-size:12px}.status{display:flex;align-items:center;gap:9px;color:var(--muted)}.dot{width:9px;height:9px;border-radius:50%;background:var(--cyan);box-shadow:0 0 0 5px color-mix(in srgb,var(--cyan) 12%,transparent)}
.grid{display:grid;grid-template-columns:minmax(0,1.6fr) minmax(300px,.8fr);gap:18px}.panel{background:color-mix(in srgb,var(--panel) 94%,transparent);border:1px solid var(--line);border-radius:18px;padding:20px;box-shadow:var(--shadow);backdrop-filter:blur(14px)}.span2{grid-column:1/-1}
.hero{display:flex;justify-content:space-between;gap:20px;align-items:flex-start;margin-bottom:20px}.eyebrow{color:var(--muted);font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.12em}.tempNow{font-size:44px;font-weight:750;letter-spacing:-.05em;line-height:1.05;margin-top:3px}.tempNow small{font-size:17px;color:var(--muted);letter-spacing:0}.pill{border:1px solid var(--line);background:var(--raised);padding:7px 11px;border-radius:999px;color:var(--muted);font-size:12px}.pill b{color:var(--text)}
.modebar{display:grid;grid-template-columns:repeat(5,1fr);gap:7px;padding:6px;background:var(--raised);border:1px solid var(--line);border-radius:13px;margin-bottom:22px}.modebar button,.preset button{border:0;background:transparent;border-radius:9px;padding:9px 7px;cursor:pointer;color:var(--muted);transition:.12s ease}.modebar button:hover,.preset button:hover{color:var(--text);background:color-mix(in srgb,var(--blue) 9%,transparent)}.modebar button.active{background:var(--panel);color:var(--text);box-shadow:0 3px 12px #0002}
.fan{padding:17px 0;border-top:1px solid var(--line)}.fan:first-of-type{border-top:0}.fanHead{display:flex;align-items:flex-end;justify-content:space-between;margin-bottom:13px}.fanName{font-weight:650}.fanMeta{font-size:12px;color:var(--muted)}.fanValue{font-size:24px;font-weight:700;font-variant-numeric:tabular-nums}.fanValue small{font-size:12px;color:var(--muted);font-weight:500}
input[type=range]{appearance:none;width:100%;height:7px;border-radius:99px;background:linear-gradient(90deg,var(--cyan),var(--blue) var(--fill),var(--raised) var(--fill));outline:none}input[type=range]::-webkit-slider-thumb{appearance:none;width:20px;height:20px;border-radius:50%;background:var(--text);border:5px solid var(--blue);box-shadow:0 2px 10px #0006;cursor:pointer}input:disabled{opacity:.38}
.preset{display:grid;grid-template-columns:repeat(5,1fr);gap:7px;margin-top:16px}.preset button{border:1px solid var(--line);background:var(--raised);font-size:12px}.sectionHead{display:flex;justify-content:space-between;align-items:center;margin-bottom:15px}.sectionHead h2{font-size:14px;margin:0}.sectionHead span{font-size:11px;color:var(--muted)}
.chart{height:160px;width:100%;display:block;overflow:visible}.chart .gridline{stroke:var(--line);stroke-width:1}.chart .tempLine{fill:none;stroke:var(--amber);stroke-width:2}.chart .gpuLine{fill:none;stroke:var(--cyan);stroke-width:2}.chart .fanLine{fill:none;stroke:var(--blue);stroke-width:2}.legend{display:flex;gap:15px;color:var(--muted);font-size:11px;flex-wrap:wrap}.legend i{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px}.empty{height:160px;display:grid;place-items:center;color:var(--muted)}
aside{align-self:start}.sensorList{display:grid;gap:8px}.sensor{display:flex;justify-content:space-between;align-items:center;padding:11px 12px;border-radius:11px;background:var(--raised);border:1px solid var(--line)}.sensor span{color:var(--muted);font-size:12px}.sensor b{font-variant-numeric:tabular-nums}
details{border-top:1px solid var(--line);padding:14px 0}details:first-of-type{border-top:0}summary{cursor:pointer;font-weight:650;list-style:none;display:flex;justify-content:space-between}summary::after{content:'+';color:var(--muted)}details[open] summary::after{content:'−'}.settings{display:grid;gap:13px;margin-top:15px}.setting{display:flex;align-items:center;justify-content:space-between;gap:20px}.setting label span{display:block;color:var(--muted);font-size:11px;font-weight:400}.setting input,.setting select{width:94px;background:var(--raised);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:7px 9px}.curveRows{display:grid;gap:7px;margin-top:14px}.curveRow{display:grid;grid-template-columns:1fr auto 1fr auto;gap:7px;align-items:center}.curveRow input{width:100%;background:var(--raised);color:var(--text);border:1px solid var(--line);padding:7px;border-radius:8px}.iconBtn{border:1px solid var(--line);background:var(--raised);border-radius:8px;padding:7px 10px;cursor:pointer}.actions{display:flex;gap:8px;margin-top:12px}.primary{border:0;background:var(--blue);color:#06111e;border-radius:9px;padding:8px 13px;font-weight:700;cursor:pointer}.secondary{border:1px solid var(--line);background:var(--raised);border-radius:9px;padding:8px 13px;cursor:pointer}.danger{color:var(--red)}
.toast{position:fixed;right:22px;bottom:22px;background:var(--text);color:var(--bg);padding:10px 14px;border-radius:10px;box-shadow:var(--shadow);opacity:0;transform:translateY(8px);pointer-events:none;transition:.18s}.toast.show{opacity:1;transform:none}
@media(max-width:800px){.grid{grid-template-columns:1fr}.span2{grid-column:auto}.shell{width:min(100% - 20px,600px);padding-top:16px}.modebar{grid-template-columns:repeat(2,1fr)}.modebar button:last-child{grid-column:1/-1}.hero{align-items:center}.tempNow{font-size:36px}}
@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto!important;transition:none!important}}
</style></head><body><main class="shell">
<header><div class="brand"><div class="logo" aria-hidden="true">◉</div><div><h1>Fan Control</h1><p>Local thermal control center</p></div></div><div class="status"><i class="dot" id="dot"></i><span id="connection">Connecting…</span><button class="iconBtn" id="theme" aria-label="Toggle theme">☼</button></div></header>
<div class="grid">
<section class="panel"><div class="hero"><div><div class="eyebrow">CPU temperature</div><div class="tempNow"><span id="primaryTemp">--</span><small>°C</small></div></div><div class="pill">Mode&nbsp; <b id="modeName">Manual</b></div></div>
<div class="modebar" id="modes"><button data-mode="manual">Manual</button><button data-profile="silent">Silent</button><button data-profile="balanced">Balanced</button><button data-profile="performance">Performance</button><button data-mode="released">EC Auto</button></div>
<div class="fan"><div class="fanHead"><div><div class="fanName">CPU fan</div><div class="fanMeta" id="fan1Raw">Raw duty --</div></div><div class="fanValue"><span id="fan1Value">0</span>% <small>set</small></div></div><input type="range" id="fan1" min="0" max="100" value="0" aria-label="CPU fan speed"></div>
<div class="fan"><div class="fanHead"><div><div class="fanName">GPU fan</div><div class="fanMeta" id="fan2Raw">Raw duty --</div></div><div class="fanValue"><span id="fan2Value">0</span>% <small>set</small></div></div><input type="range" id="fan2" min="0" max="100" value="0" aria-label="GPU fan speed"></div>
<div class="preset" id="presets"><button data-value="0">Stop</button><button data-value="25">Quiet</button><button data-value="50">Medium</button><button data-value="75">High</button><button data-value="100">Max</button></div></section>

<aside class="panel"><div class="sectionHead"><h2>Live sensors</h2><span id="sensorCount">0 detected</span></div><div class="sensorList" id="sensors"><div class="empty">Waiting for sensors…</div></div></aside>

<section class="panel span2"><div class="sectionHead"><h2>30-minute history</h2><div class="legend"><span><i style="background:var(--amber)"></i>CPU temp</span><span><i style="background:var(--cyan)"></i>GPU temp</span><span><i style="background:var(--blue)"></i>Fan duty</span></div></div><div id="chartEmpty" class="empty">Collecting history…</div><svg id="chart" class="chart" viewBox="0 0 1000 160" preserveAspectRatio="none" hidden aria-label="CPU temperature, GPU temperature, and fan duty history"><line class="gridline" x1="0" y1="40" x2="1000" y2="40"/><line class="gridline" x1="0" y1="80" x2="1000" y2="80"/><line class="gridline" x1="0" y1="120" x2="1000" y2="120"/><polyline id="tempLine" class="tempLine"/><polyline id="gpuLine" class="gpuLine"/><polyline id="fanLine" class="fanLine"/></svg></section>

<section class="panel span2"><details><summary>Custom fan curve</summary><div class="curveRows" id="curveRows"></div><div class="actions"><button class="secondary" id="addPoint">Add point</button><button class="primary" id="applyCurve">Apply curve</button></div></details>
<details><summary>Safety &amp; preferences</summary><div class="settings"><div class="setting"><label>Link fans<span>Move both sliders together</span></label><input id="linked" type="checkbox" checked></div><div class="setting"><label>Maximum duty<span>Noise cap; critical cooling bypasses it</span></label><input id="maxDuty" type="number" min="20" max="198"></div><div class="setting"><label>Curve hysteresis<span>Prevents rapid speed hunting</span></label><input id="hysteresis" type="number" min="0" max="30"></div><div class="setting"><label>Critical temperature<span>Forces safe maximum duty</span></label><input id="criticalTemp" type="number" min="70" max="110"></div><div class="setting"><label>Temperature alerts<span>Browser alert at critical temperature</span></label><button class="secondary" id="notifications">Enable</button></div><div class="setting"><label>Hardware control<span>Return control to firmware immediately</span></label><button class="secondary danger" id="release">Release to EC</button></div></div></details></section>
</div></main><div class="toast" id="toast" role="status"></div>
<script>
const $=id=>document.getElementById(id), ui={linked:true,state:null,notified:false};
const esc=value=>String(value).replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const toast=message=>{const el=$('toast');el.textContent=message;el.classList.add('show');clearTimeout(toast.timer);toast.timer=setTimeout(()=>el.classList.remove('show'),2200)};
async function api(path,options={}){const response=await fetch(path,{...options,headers:{'Content-Type':'application/json',...(options.headers||{})}});if(!response.ok)throw new Error((await response.json().catch(()=>({}))).error||`Request failed (${response.status})`);return response.json()}
const post=(path,body)=>api(path,{method:'POST',body:JSON.stringify(body)});
const dutyPct=(duty,cap)=>Math.min(100,Math.round(100*duty/cap));
function setRange(id,value){const el=$(id);el.value=value;el.style.setProperty('--fill',`${value}%`);$(`${id}Value`).textContent=value}
async function setFan(fan,value){setRange(`fan${fan}`,value);await post('/set',{fan,pct:value});if(ui.linked){const other=fan===1?2:1;setRange(`fan${other}`,value);await post('/set',{fan:other,pct:value})}}
for(const fan of [1,2])$('fan'+fan).addEventListener('input',event=>setFan(fan,+event.target.value).catch(e=>toast(e.message)));
$('linked').addEventListener('change',event=>ui.linked=event.target.checked);
$('presets').addEventListener('click',event=>{const button=event.target.closest('button[data-value]');if(button)setFan(1,+button.dataset.value).catch(e=>toast(e.message))});
$('modes').addEventListener('click',async event=>{const button=event.target.closest('button');if(!button)return;try{if(button.dataset.profile)await post('/profile',{profile:button.dataset.profile});else await post('/mode',{mode:button.dataset.mode});await update();toast(button.textContent+' mode enabled')}catch(e){toast(e.message)}});
$('release').addEventListener('click',()=>post('/mode',{mode:'released'}).then(update).catch(e=>toast(e.message)));
for(const id of ['maxDuty','hysteresis','criticalTemp'])$(id).addEventListener('change',event=>{const keys={maxDuty:'max_duty',hysteresis:'hysteresis',criticalTemp:'critical_temp'};post('/config',{[keys[id]]:+event.target.value}).then(()=>toast('Setting saved')).catch(e=>toast(e.message))});
function renderCurve(curve){$('curveRows').innerHTML=curve.map(([temp,duty],index)=>`<div class="curveRow"><input class="curveTemp" type="number" min="0" max="150" value="${temp}" aria-label="Temperature point ${index+1}"><span>°C →</span><input class="curveDuty" type="number" min="0" max="198" value="${duty}" aria-label="Duty point ${index+1}"><button class="iconBtn danger" data-remove="${index}" aria-label="Remove point">×</button></div>`).join('')}
$('curveRows').addEventListener('click',event=>{if(event.target.dataset.remove!==undefined){event.target.closest('.curveRow').remove()}});
$('addPoint').addEventListener('click',()=>{const rows=[...document.querySelectorAll('.curveRow')],last=rows.at(-1),curve=rows.map(r=>[+r.querySelector('.curveTemp').value,+r.querySelector('.curveDuty').value]);curve.push(last?[Math.min(150,curve.at(-1)[0]+5),Math.min(198,curve.at(-1)[1]+10)]:[60,50]);renderCurve(curve)});
$('applyCurve').addEventListener('click',()=>{const curve=[...document.querySelectorAll('.curveRow')].map(r=>[+r.querySelector('.curveTemp').value,+r.querySelector('.curveDuty').value]);post('/custom',{curve}).then(()=>{toast('Custom curve active');update()}).catch(e=>toast(e.message))});
function chart(history){if(history.length<2)return;const start=history[0].time,end=history.at(-1).time||start+1,x=p=>1000*(p.time-start)/(end-start||1),tempY=value=>(150-Math.min(110,value)*1.35).toFixed(1),tempPoints=history.filter(p=>p.temp!=null).map(p=>`${x(p).toFixed(1)},${tempY(p.temp)}`).join(' '),gpuPoints=history.filter(p=>p.gpu_temp!=null).map(p=>`${x(p).toFixed(1)},${tempY(p.gpu_temp)}`).join(' '),fanPoints=history.map(p=>`${x(p).toFixed(1)},${(150-Math.min(198,p.fan1)*.72).toFixed(1)}`).join(' ');$('tempLine').setAttribute('points',tempPoints);$('gpuLine').setAttribute('points',gpuPoints);$('fanLine').setAttribute('points',fanPoints);$('chart').hidden=false;$('chartEmpty').hidden=true}
function render(s){ui.state=s;const temp=s.primary_temp;$('primaryTemp').textContent=temp==null?'--':temp.toFixed(1);$('modeName').textContent=s.mode==='released'?'EC Auto':s.mode==='curve'?s.profile[0].toUpperCase()+s.profile.slice(1):'Manual';$('connection').textContent=Date.now()/1000-s.updated<3?'Live':'Readback delayed';$('dot').style.background=Date.now()/1000-s.updated<3?'var(--cyan)':'var(--amber)';const manual=s.mode==='manual';for(const fan of [1,2]){const duty=s['fan'+fan];$(`fan${fan}Raw`).textContent=`Raw duty ${duty} · actual ${dutyPct(duty,s.max_duty)}%`;$(`fan${fan}`).disabled=!manual;if(document.activeElement!==$(`fan${fan}`))setRange(`fan${fan}`,s.targets[fan])}$('presets').querySelectorAll('button').forEach(b=>b.disabled=!manual);document.querySelectorAll('#modes button').forEach(b=>b.classList.toggle('active',b.dataset.mode===s.mode||s.mode==='curve'&&b.dataset.profile===s.profile));$('maxDuty').value=s.max_duty;$('hysteresis').value=s.hysteresis;$('criticalTemp').value=s.critical_temp;if(!$('curveRows').children.length)renderCurve(s.custom_curve);$('sensorCount').textContent=`${s.temps.length} detected`;$('sensors').innerHTML=s.temps.length?s.temps.map(t=>`<div class="sensor"><span>${esc(t.name)} · ${esc(t.label)}</span><b>${t.temp.toFixed(1)}°C</b></div>`).join(''):'<div class="empty">No hwmon sensors found</div>';if(temp>=s.critical_temp&&!ui.notified&&Notification.permission==='granted'){new Notification('Fan Control',{body:`Critical CPU temperature: ${temp.toFixed(1)}°C. Maximum cooling engaged.`});ui.notified=true}if(temp<s.critical_temp-5)ui.notified=false}
async function update(){try{render(await api('/snapshot'));const h=await api('/history');chart(h.history)}catch(e){$('connection').textContent='Disconnected';$('dot').style.background='var(--red)'}}
$('notifications').addEventListener('click',async()=>{if(!('Notification'in window))return toast('Notifications are not supported');const permission=await Notification.requestPermission();toast(permission==='granted'?'Temperature alerts enabled':'Notifications not enabled')});
function applyTheme(theme){document.documentElement.dataset.theme=theme;localStorage.setItem('fan-theme',theme);$('theme').textContent=theme==='dark'?'☼':'☾'}applyTheme(localStorage.getItem('fan-theme')||'dark');$('theme').addEventListener('click',()=>applyTheme(document.documentElement.dataset.theme==='dark'?'light':'dark'));
update();setInterval(update,2000);
</script></body></html>'''
HTML_BYTES = HTML.encode()


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def send_value(self, value, status=200, content_type="application/json"):
        body = value if isinstance(value, bytes) else json.dumps(value, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            self.send_value(HTML_BYTES, content_type="text/html; charset=utf-8")
        elif path == "/snapshot":
            self.send_value(snapshot())
        elif path == "/history":
            with STATE_LOCK:
                self.send_value({"history": list(HISTORY)})
        else:
            self.send_value({"error": "not found"}, 404)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 64_000:
                raise ValueError("request too large")
            value = json.loads(self.rfile.read(length)) if length else {}
            if not isinstance(value, dict):
                raise ValueError("JSON body must be an object")
            return value
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid request: {exc}") from exc

    def do_POST(self):
        try:
            host = self.headers.get("Host", "")
            origin = self.headers.get("Origin")
            if not (host == "localhost" or host.startswith("localhost:") or host == "127.0.0.1" or host.startswith("127.0.0.1:")):
                self.send_value({"error": "local requests only"}, 403)
                return
            if origin and origin != f"http://{host}":
                self.send_value({"error": "cross-origin request blocked"}, 403)
                return
            body = self.read_json()
            hardware_action = None
            with STATE_LOCK:
                if self.path == "/set":
                    fan, pct = int(body.get("fan", 0)), int(body.get("pct", -1))
                    if fan not in (1, 2) or not 0 <= pct <= 100:
                        raise ValueError("fan must be 1 or 2 and percent 0-100")
                    state["targets"][fan] = pct
                    state["mode"] = "manual"
                    hardware_action = ("set", fan, round(pct * state["max_duty"] / 100))
                elif self.path == "/profile":
                    profile = body.get("profile")
                    if profile not in PROFILES:
                        raise ValueError("unknown profile")
                    state["profile"], state["mode"] = profile, "curve"
                    hardware_action = ("lock",)
                elif self.path == "/mode":
                    mode = body.get("mode")
                    if mode not in ("manual", "curve", "released"):
                        raise ValueError("unknown mode")
                    state["mode"] = mode
                    hardware_action = ("release",) if mode == "released" else ("lock",)
                elif self.path == "/custom":
                    state["custom_curve"] = normalize_curve(body.get("curve"))
                    state["profile"], state["mode"] = "custom", "curve"
                    hardware_action = ("lock",)
                elif self.path == "/config":
                    if "max_duty" in body:
                        state["max_duty"] = max(20, min(SAFE_MAX_DUTY, int(body["max_duty"])))
                    if "hysteresis" in body:
                        state["hysteresis"] = max(0, min(30, int(body["hysteresis"])))
                    if "critical_temp" in body:
                        state["critical_temp"] = max(70, min(110, int(body["critical_temp"])))
                else:
                    self.send_value({"error": "not found"}, 404)
                    return
            if hardware_action:
                if hardware_action[0] == "set":
                    lock_manual()
                    write_duty(hardware_action[1], hardware_action[2])
                elif hardware_action[0] == "release":
                    release_manual()
                else:
                    lock_manual()
            save_config()
            self.send_value({"ok": True})
        except (ValueError, TypeError, OSError) as exc:
            self.send_value({"error": str(exc)}, 400)


def stop_daemon():
    subprocess.run(["systemctl", "stop", "fan-daemon"], capture_output=True)
    for _ in range(50):
        result = subprocess.run(["systemctl", "is-active", "fan-daemon"], capture_output=True, text=True)
        if result.stdout.strip() != "active":
            break
        time.sleep(0.05)


def shutdown(*_):
    global FD
    if STOP.is_set():
        return
    STOP.set()
    if DEMO:
        release_manual()
    elif FD is not None:
        with EC_LOCK:
            try:
                fcntl.ioctl(FD, W_AUTO)
            except OSError:
                pass
            os.close(FD)
            FD = None
    if not DEMO:
        subprocess.run(["systemctl", "start", "fan-daemon"], capture_output=True)


def main():
    global FD, DEMO
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", help="run with simulated hardware")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--port", type=int, default=4444)
    parser.add_argument("--device", help="kernel device path (or set FAN_CONTROL_DEVICE)")
    args = parser.parse_args()
    DEMO = args.demo
    if not DEMO:
        stop_daemon()
        try:
            FD = os.open(args.device or find_ec_device(), os.O_RDWR)
        except PermissionError:
            sys.exit("need root")
        except FileNotFoundError as exc:
            sys.exit(str(exc))
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    if state["mode"] == "released":
        release_manual()
    else:
        lock_manual()
    server = None
    try:
        for target in (sensor_loop, readback_loop, control_loop, history_loop):
            threading.Thread(target=target, daemon=True).start()
        server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        if not args.no_browser:
            webbrowser.open(f"http://127.0.0.1:{args.port}")
        while not STOP.wait(1):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        if server:
            server.shutdown()
        shutdown()


if __name__ == "__main__":
    main()
