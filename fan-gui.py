#!/usr/bin/env python3
"""Web UI for /dev/tuxedo_io fan control. Listens on 127.0.0.1:4444."""
import ctypes, fcntl, http.server, json, os, pathlib, signal, subprocess, sys, threading, time, webbrowser

MAGIC_RD = 0xEF; MAGIC_WR = 0xF0
IOC_R, IOC_W, SZ = 2, 1, 8
def ioc(d,t,n,s): return (d<<30)|(t<<8)|(n<<0)|(s<<16)

R_FS1   = ioc(IOC_R, MAGIC_RD, 0x10, SZ)
R_FS2   = ioc(IOC_R, MAGIC_RD, 0x11, SZ)
R_TEMP  = ioc(IOC_R, MAGIC_RD, 0x12, SZ)
R_TEMP2 = ioc(IOC_R, MAGIC_RD, 0x13, SZ)
W_FS1   = ioc(IOC_W, MAGIC_WR, 0x10, SZ)
W_FS2   = ioc(IOC_W, MAGIC_WR, 0x11, SZ)
W_MODE  = ioc(IOC_W, MAGIC_WR, 0x12, SZ)

FD = os.open('/dev/tuxedo_io', os.O_RDWR)
BUF = (ctypes.c_int64)()

def rd(cmd):
    BUF.value = 0; fcntl.ioctl(FD, cmd, BUF, True); return BUF.value & 0xFF

# # ponytail: three built-in profiles + a `custom` slot the user can edit.
# Each curve is a list of (temp_c, pwm_duty) interpolated linearly. Max
# duty is `state['max_duty']` (default 198 — EC firmware wraps at 200).
DEFAULT_PROFILES = {
    'silent': [
        (0,0),(50,0),(60,0),(70,60),(75,90),(80,120),(85,150),(90,170),(95,198),(110,198),
    ],
    'balanced': [
        (0,0),(50,0),(60,50),(70,100),(75,130),(80,150),(85,170),(90,180),(95,198),(110,198),
    ],
    'performance': [
        (0,0),(50,0),(60,80),(70,140),(75,170),(80,180),(85,198),(90,198),(95,198),(110,198),
    ],
    'custom': [
        (0,0),(60,0),(70,80),(80,140),(90,180),(95,198),(110,198),
    ],
}

def find_k10():
    for h in sorted(pathlib.Path('/sys/class/hwmon').glob('hwmon*')):
        if (h/'name').read_text().strip() == 'k10temp': return h
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

# # ponytail: 3-mode state machine. The previous version had a 10-second
# override window that raced the poller. Cleaner: one mode at a time.
#  - mode='curve'   : poller writes the live curve duty. UI sliders/presets
#                      are read-only here. Profile is what controls the fan.
#  - mode='manual'  : poller writes `state['targets']`. Sliders/presets drive
#                      the fan. Stays in manual until a profile is picked.
#  - mode='released': poller no-ops. EC's own auto-curve takes over.
#  - mode='custom'  : same as 'curve' but uses the editable custom curve.
state = {
    'mode': 'curve',
    'profile': 'balanced',
    'targets': {1: 0, 2: 0},
    'max_duty': 198,
    'hysteresis': 5,
    'custom_curve': list(DEFAULT_PROFILES['custom']),
}

def lock_manual():
    BUF.value = 0x40; fcntl.ioctl(FD, W_MODE, BUF, True)
def release_manual():
    BUF.value = 0; fcntl.ioctl(FD, W_MODE, BUF, True)

def write_duty(f, duty):
    BUF.value = int(duty)
    fcntl.ioctl(FD, (W_FS1, W_FS2)[f-1], BUF, True)

def sensors():
    out = []
    for h in sorted(pathlib.Path('/sys/class/hwmon').glob('hwmon*')):
        try: nm = (h/'name').read_text().strip()
        except Exception: continue
        for t in sorted(h.glob('temp*_input')):
            try: out.append({'name': nm, 'label': t.stem, 'temp': int(t.read_text().strip())/1000})
            except Exception: pass
    return out

def current_curve():
    if state['profile'] == 'custom':
        return state['custom_curve']
    return DEFAULT_PROFILES[state['profile']]

def curve_duty():
    """Return the duty the curve wants right now, or None if temp is unknown."""
    hw = find_k10()
    t = cpu_temp(hw)
    if t is None:
        t = float(rd(R_TEMP))
    if t is None: return None
    pwm = float(interp(float(t), current_curve()))
    return max(0, min(state['max_duty'], int(round(pwm))))

def snapshot():
    return {
        'fan1': rd(R_FS1), 'fan2': rd(R_FS2),
        'ec_temp1': rd(R_TEMP), 'ec_temp2': rd(R_TEMP2),
        'targets': dict(state['targets']),
        'mode': state['mode'],
        'profile': state['profile'],
        'max_duty': state['max_duty'],
        'custom_curve': list(state['custom_curve']),
        'temps': sensors(),
    }

def poll():
    # # ponytail: at 50Hz the poller keeps up with any 100ms-class EC settle
    # delay the driver has. Reads are free (cached), writes are unconditional
    # in 'curve' and 'manual' modes — no hysteresis in those modes, the
    # curve is already the source of truth.
    if state['mode'] == 'released':
        return
    if state['mode'] == 'curve' or state['mode'] == 'custom':
        d = curve_duty()
        if d is None: return
        for f in (1, 2):
            write_duty(f, d)
    elif state['mode'] == 'manual':
        for f in (1, 2):
            write_duty(f, max(0, min(state['max_duty'], int(state['targets'][f]) * 2)))

def poller():
    while True:
        poll()
        time.sleep(0.02)  # 50Hz; EC settle is ~100ms but writes are idempotent

# # ponytail: GUI stops the daemon before opening the EC device. `systemctl
# stop` returns once the signal is sent — poll is-active until the unit
# really leaves 'active' so the daemon's last write doesn't race us.
subprocess.run(["systemctl", "stop", "fan-daemon"], capture_output=True)
for _ in range(50):
    r = subprocess.run(["systemctl", "is-active", "fan-daemon"], capture_output=True, text=True)
    if r.stdout.strip() != "active": break
    time.sleep(0.05)
subprocess.run(["systemctl", "reset-failed", "fan-daemon"], capture_output=True)
lock_manual()
threading.Thread(target=poller, daemon=True).start()

HTML = r"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="color-scheme" content="dark"><title>Fan Control</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#0d1117;--card:#161b22;--border:#30363d;--text:#e6edf3;--dim:#8b949e;--accent:#58a6ff;--accent2:#1f6feb;--green:#3fb950;--orange:#d29922;--red:#f85149;--gold:#d29922}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;padding:24px;min-height:100vh}
.wrap{max-width:1100px;margin:0 auto;display:grid;grid-template-columns:1fr 360px;gap:20px}
@media(max-width:900px){.wrap{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:24px}
h1{font-size:22px;font-weight:600;margin-bottom:4px}
h2{font-size:14px;font-weight:600;color:var(--dim);text-transform:uppercase;letter-spacing:.5px;margin-bottom:12px}
.sub{color:var(--dim);font-size:13px;margin-bottom:20px}
.row{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px}
.row span{font-variant-numeric:tabular-nums;font-weight:500}
.row .v{color:var(--text);font-size:16px}
.row .d{color:var(--dim);font-size:12px;margin-left:6px}
.group{margin-bottom:22px}
input[type=range]{-webkit-appearance:none;appearance:none;width:100%;height:6px;border-radius:3px;background:#21262d;outline:none;margin-top:4px}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:18px;height:18px;border-radius:50%;background:var(--accent);cursor:pointer;border:2px solid var(--accent2);transition:.1s}
input[type=range]::-webkit-slider-thumb:hover{transform:scale(1.15)}
input[type=range]:disabled::-webkit-slider-thumb{background:#30363d;border-color:#30363d;cursor:not-allowed;transform:none}
input[type=range]:disabled{opacity:.5}
.bar{height:8px;border-radius:4px;background:#21262d;overflow:hidden;margin-top:8px}
.bar > div{height:100%;background:linear-gradient(90deg,var(--green),var(--orange),var(--red));transition:width .1s}
.bar.curve > div{background:linear-gradient(90deg,var(--green),var(--gold))}
.temp-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}
.temp{background:#0d1117;border:1px solid var(--border);border-radius:8px;padding:12px}
.temp .l{color:var(--dim);font-size:11px;text-transform:uppercase}
.temp .t{font-size:22px;font-weight:600;font-variant-numeric:tabular-nums;margin-top:4px}
.btn{background:#21262d;color:var(--text);border:1px solid var(--border);border-radius:6px;padding:8px 14px;font-size:13px;cursor:pointer}
.btn:hover{background:#30363d}
.btn.active{background:var(--accent2);border-color:var(--accent2);color:#fff}
.btn.warn{background:var(--red);border-color:var(--red);color:#fff}
.btn[disabled]{opacity:.5;cursor:not-allowed}
.btn-row{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
.setting{display:flex;justify-content:space-between;align-items:flex-start;padding:10px 0;border-bottom:1px solid var(--border);gap:12px}
.setting > div{flex:1}
.setting label{font-size:13px;color:var(--text);display:block;margin-bottom:2px}
.setting .desc{font-size:11px;color:var(--dim);line-height:1.4;margin-top:2px}
.setting input[type=number]{width:80px;background:#0d1117;border:1px solid var(--border);color:var(--text);padding:4px 8px;border-radius:4px;font-family:inherit;font-size:13px;text-align:right}
.setting select{background:#0d1117;border:1px solid var(--border);color:var(--text);padding:4px 8px;border-radius:4px;font-family:inherit;font-size:13px}
.preset{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
.preset button{flex:1;min-width:60px;background:#21262d;color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:12px;cursor:pointer}
.preset button:hover{background:#30363d}
.preset button.active{background:var(--accent2);border-color:var(--accent2);color:#fff}
.live-dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--green);margin-right:6px;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.tag{display:inline-block;background:#21262d;border:1px solid var(--border);border-radius:4px;padding:2px 6px;font-size:11px;color:var(--dim);margin-left:8px}
.tag.curve{background:#3fb95022;border-color:#3fb95055;color:var(--green)}
.tag.manual{background:#d2992222;border-color:#d2992255;color:var(--gold)}
.tag.released{background:#f8514922;border-color:#f8514955;color:var(--red)}
.badge{display:inline-block;background:#21262d;border:1px solid var(--border);border-radius:4px;padding:1px 5px;font-size:10px;color:var(--dim);margin-left:4px;vertical-align:middle}
.suggest{font-size:12px;color:var(--dim);margin-top:6px;line-height:1.5}
.curve-table{font-size:11px;color:var(--dim);width:100%;margin-top:8px;border-collapse:collapse}
.curve-table td{padding:2px 4px;border-bottom:1px solid var(--border);font-variant-numeric:tabular-nums}
.curve-table input{width:54px;background:#0d1117;border:1px solid var(--border);color:var(--text);padding:2px 4px;border-radius:3px;font-family:inherit;font-size:11px;text-align:right}
.title-tip{cursor:help;border-bottom:1px dotted var(--dim)}
</style></head><body>
<div class="wrap">
<div class="card">
<h1>⚡ Fan Control</h1>
<div class="sub"><span class="live-dot"></span>Live <span id="modeTag" class="tag curve">curve</span><span class="tag" id="profileTag">balanced</span></div>

<div class="group">
<div class="row"><label>CPU Fan <span class="badge" id="cpuPct"></span></label><span><span class="v" id="v1">0</span>% <span class="d" id="a1">(read 0)</span></span></div>
<input type="range" id="f1" min="0" max="100" value="0" disabled>
<div class="bar curve" id="barWrap1"><div id="bar1" style="width:0%"></div></div>
</div>

<div class="group">
<div class="row"><label>GPU Fan <span class="badge" id="gpuPct"></span></label><span><span class="v" id="v2">0</span>% <span class="d" id="a2">(read 0)</span></span></div>
<input type="range" id="f2" min="0" max="100" value="0" disabled>
<div class="bar curve" id="barWrap2"><div id="bar2" style="width:0%"></div></div>
</div>

<div class="group">
<h2><span class="title-tip" title="Three built-in profiles plus a custom curve you edit. Click a profile to enter curve mode; the poller drives the fan from CPU temp.">Profile</span></h2>
<div class="btn-row" id="profiles">
<button class="btn" data-profile="silent" title="Stays silent below 60°C. Aggressive ramp after 70°C. Best for quiet environments.">🤫 Silent</button>
<button class="btn active" data-profile="balanced" title="Default. Balanced between noise and cooling. Reasonable fan noise for typical workloads.">⚖️ Balanced</button>
<button class="btn" data-profile="performance" title="Keeps fans at higher duty. Better for sustained heavy loads, gaming, video encoding.">🚀 Performance</button>
<button class="btn" data-profile="custom" title="Edit your own curve. Click to enter edit mode.">🛠 Custom</button>
<button class="btn" data-profile="manual" title="Manual mode. The curve stops driving. The poller holds whatever duty is set by the slider/presets/Hot & Silent. Pick a profile to go back to curve mode.">🎛️ Manual</button>
</div>
</div>
<div class="suggest" id="curveHint">CPU temp drives the fan. Sliders are disabled in curve mode — pick a preset or switch to manual.</div>
</div>

<div class="group" id="customEditor" style="display:none">
<h2><span class="title-tip" title="Each row: (CPU temp °C, fan duty 0-198). Linear interpolation between rows. The poller reads this and writes the fan.">Custom Curve</span></h2>
<table class="curve-table" id="curveTable"></table>
<div class="btn-row" style="margin-top:8px">
<button class="btn" id="addRow">+ Add row</button>
<button class="btn warn" id="rmRow">- Remove last</button>
<button class="btn primary" id="applyCurve">Apply</button>
</div>
</div>

<div class="group">
<h2><span class="title-tip" title="Drag a slider to take manual control. The poller will write your value at 50Hz, no curve drift.">Manual Override</span></h2>
<div class="preset" id="presets">
<button data-v="0" title="Set both fans to 0%.">Stop</button>
<button data-v="25" title="25% duty.">25%</button>
<button data-v="50" title="50% duty.">50%</button>
<button data-v="75" title="75% duty.">75%</button>
<button data-v="100" title="Max duty (clamped to the cap).">Max</button>
</div>
<div class="btn-row">
<button class="btn" id="hotsilent" title="Lock both fans at the hardware's minimum stable speed. Ignores CPU temperature — even a 90°C CPU stays quiet. The poller fights any drift back to the EC's own auto-curve.">🥵 Hot &amp; Silent</button>
<button class="btn" id="link" title="When on, dragging fan 1 also moves fan 2 and vice versa. Off = independent control.">🔗 Link fans</button>
<button class="btn warn" id="restore" title="Hand control back to the EC's own auto-curve. The poller stops writing.">Release control</button>
</div>
<div class="suggest" id="manualHint" style="display:none">Manual mode active. The poller writes your slider value 50× per second. Pick a profile to go back to curve mode.</div>
</div>

<div class="group"><h2>Live Sensors</h2><div class="temp-grid" id="temps"></div>
<div class="sub" style="margin-top:12px">EC: <span id="ec1">--</span>°C / <span id="ec2">--</span>°C</div></div>
</div>

<div class="card">
<h2>Settings</h2>
<div class="setting">
<div>
<label><span class="title-tip" title="Maximum fan duty (0-198). The EC firmware wraps at 200, so 198 is the safe ceiling. Lower this if 100% sounds too loud.">Max duty cap</span></label>
<div class="desc">Upper bound on fan duty. EC firmware wraps at 200; 198 is the safe ceiling.</div>
</div>
<input type="number" id="maxduty" min="0" max="198" step="1" value="198">
</div>
<div class="setting">
<div>
<label>Theme</label>
<div class="desc">Visual style. Dark is the default; Midnight is OLED-friendly; Light for daytime.</div>
</div>
<select id="theme"><option value="dark">Dark</option><option value="midnight">Midnight</option><option value="light">Light</option></select>
</div>
<div class="setting">
<div>
<label>Show °F</label>
<div class="desc">Display temperatures in Fahrenheit instead of Celsius.</div>
</div>
<input type="checkbox" id="fahr" style="width:18px;height:18px">
</div>
<div class="setting">
<div>
<label><span class="title-tip" title="How often the EC read-back is shown in the UI. Independent of the 50Hz poller that writes the fan.">UI refresh rate</span></label>
<div class="desc">How often the UI polls the EC for read-back. The fan write rate is fixed at 50Hz.</div>
</div>
<input type="number" id="refresh" min="1" max="10" step="1" value="1">
</div>
</div>
</div>

<script>
const $=id=>document.getElementById(id);
let linked=true, fahrenheit=false, refreshMs=1000;

function fmt(t){return fahrenheit?(t*9/5+32).toFixed(1):t.toFixed(1)}
const pct = d => Math.round(d / 2);

function post(url, body){
  return fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})}).catch(()=>{});
}

function setFan(f,v){
  post('/set',{fan:f,pct:v});
  state.targets[f] = v;
  if (linked) {
    const other = f === 1 ? 2 : 1;
    post('/set',{fan:other,pct:v});
    state.targets[other] = v;
  }
}

let state = { targets: {1:0, 2:0}, mode:'curve', profile:'balanced' };

async function switchProfile(p){
  if (state.profile === p) return;
  await post('/profile',{profile:p});
  state.profile = p;
  state.mode = (p === 'custom') ? 'custom' : 'curve';
  refreshUI();
}

async function setMode(m){
  await post('/mode',{mode:m});
  state.mode = m;
  refreshUI();
}

document.querySelectorAll('#profiles button').forEach(b=>{
  b.addEventListener('click', async () => {
    const p = b.dataset.profile;
    if (p === 'manual') {
      // # ponytail: Manual is a mode toggle, not a profile change. The
      // last-selected curve profile stays in state so resuming is one click.
      await setMode('manual');
    } else {
      await switchProfile(p);
    }
  });
});

document.querySelectorAll('#presets button').forEach(b=>{
  b.addEventListener('click',async ()=>{
    const v = +b.dataset.v;
    await setMode('manual');
    $('f1').value = v; $('v1').textContent = v;
    $('f2').value = v; $('v2').textContent = v;
    setFan(1, v);
  });
});

$('f1').addEventListener('input',e=>{const v=+e.target.value;$('v1').textContent=v;setFan(1,v)});
$('f2').addEventListener('input',e=>{const v=+e.target.value;$('v2').textContent=v;setFan(2,v)});

$('link').addEventListener('click',()=>{
  linked=!linked;
  $('link').textContent = linked?'🔗 Linked':'⛓ Unlinked';
  $('link').classList.toggle('active', linked);
});

$('restore').addEventListener('click',()=>setMode('released'));

// # ponytail: "Hot & Silent" — force both fans to the EC's actual hardware
// floor. 25% slider (= duty 50) looks like a floor but the EC firmware
// guards against very-low speeds and intermittently ramps to 0x3c (60 duty,
// = 30% slider) for ~3 minutes before settling. Asking for 0x3c directly
// avoids the ramp — 30% slider is the value the EC accepts and holds.
$('hotsilent').addEventListener('click', async () => {
  await setMode('manual');
  const v = 30;
  $('f1').value = v; $('v1').textContent = v;
  $('f2').value = v; $('v2').textContent = v;
  setFan(1, v);
});

$('maxduty').addEventListener('change', e=>post('/config',{max_duty:+e.target.value}));

$('fahr').addEventListener('change',e=>{fahrenheit=e.target.checked;update()});

$('refresh').addEventListener('change', e=>{
  refreshMs = Math.max(500, +e.target.value * 1000);
  clearInterval(refreshTimer);
  refreshTimer = setInterval(update, refreshMs);
});

function theme(t){
  const light = {'--bg':'#f6f8fa','--card':'#fff','--text':'#1f2328','--dim':'#59636e','--border':'#d1d9e0'};
  const dark = {'--bg':'#000','--card':'#0a0a0a'};
  const vars = t==='light' ? light : t==='midnight' ? dark : null;
  ['--bg','--card','--text','--dim','--border'].forEach(k=>{
    if (vars && k in vars) document.documentElement.style.setProperty(k, vars[k]);
    else document.documentElement.style.removeProperty(k);
  });
}
$('theme').addEventListener('change', e=>theme(e.target.value));

// Custom curve editor
function renderCurveTable(curve){
  const t = $('curveTable');
  t.innerHTML = curve.map(([temp,pwm],i)=>
    `<tr><td>°C <input type="number" class="ct-t" data-i="${i}" value="${temp}"></td>`+
    `<td>→</td>`+
    `<td>duty <input type="number" class="ct-d" data-i="${i}" min="0" max="198" value="${pwm}"></td></tr>`
  ).join('');
}

document.querySelectorAll('#profiles button[data-profile=custom]').forEach(b=>{
  b.addEventListener('click', async () => {
    await switchProfile('custom');
    $('customEditor').style.display = 'block';
    fetch('/snapshot').then(r=>r.json()).then(s=>{
      renderCurveTable(s.custom_curve);
    });
  });
});

$('addRow').addEventListener('click',()=>{
  const rows = [...$('curveTable').querySelectorAll('tr')];
  if (rows.length === 0) return;
  const last = rows[rows.length-1];
  const t = +last.querySelector('.ct-t').value;
  const d = +last.querySelector('.ct-d').value;
  const newRow = document.createElement('tr');
  newRow.innerHTML = `<td>°C <input type="number" class="ct-t" data-i="${rows.length}" value="${t+5}"></td>`+
    `<td>→</td>`+
    `<td>duty <input type="number" class="ct-d" data-i="${rows.length}" min="0" max="198" value="${Math.min(198, d+10)}"></td>`;
  $('curveTable').appendChild(newRow);
});
$('rmRow').addEventListener('click',()=>{
  const rows = $('curveTable').querySelectorAll('tr');
  if (rows.length > 1) rows[rows.length-1].remove();
});
$('applyCurve').addEventListener('click',()=>{
  const rows = [...$('curveTable').querySelectorAll('tr')];
  const curve = rows.map(r => [
    +r.querySelector('.ct-t').value,
    +r.querySelector('.ct-d').value
  ]).filter(([t,d]) => Number.isFinite(t) && Number.isFinite(d));
  curve.sort((a,b) => a[0] - b[0]);
  renderCurveTable(curve);
  post('/custom',{curve});
});

function refreshUI(){
  document.querySelectorAll('#profiles button').forEach(b=>{
    // # ponytail: 'manual' is a mode, not a profile — highlight it when
    // mode === 'manual' instead of when it matches state.profile.
    const isActive = b.dataset.profile === 'manual'
      ? state.mode === 'manual'
      : b.dataset.profile === state.profile;
    b.classList.toggle('active', isActive);
  });
  const inCurve = state.mode === 'curve' || state.mode === 'custom';
  const inManual = state.mode === 'manual';
  const inReleased = state.mode === 'released';
  ['f1','f2'].forEach(id=>$(id).disabled = inCurve || inReleased);
  document.querySelectorAll('#presets button').forEach(b=>b.disabled = inReleased);
  $('restore').disabled = inReleased;
  $('link').disabled = inReleased;
  $('customEditor').style.display = (state.mode === 'custom') ? 'block' : 'none';
  $('manualHint').style.display = inManual ? 'block' : 'none';
  const tag = $('modeTag');
  if (inReleased) {
    tag.className = 'tag released';
    tag.textContent = 'released';
  } else if (inManual) {
    tag.className = 'tag manual';
    tag.textContent = 'manual';
  } else if (state.mode === 'custom') {
    tag.className = 'tag curve';
    tag.textContent = 'custom';
  } else {
    tag.className = 'tag curve';
    tag.textContent = 'curve: ' + state.profile;
  }
  $('profileTag').textContent = state.profile;
}

function update(){
  fetch('/snapshot').then(r=>r.json()).then(s=>{
    $('a1').textContent=`(read ${s.fan1} / ${pct(s.fan1)}%)`;
    $('a2').textContent=`(read ${s.fan2} / ${pct(s.fan2)}%)`;
    $('bar1').style.width=`${pct(s.fan1)}%`;
    $('bar2').style.width=`${pct(s.fan2)}%`;
    $('ec1').textContent=fmt(s.ec_temp1);
    $('ec2').textContent=fmt(s.ec_temp2);
    state.mode = s.mode;
    state.profile = s.profile;
    state.targets = s.targets;
    // Update slider positions to reflect the live duty in curve mode
    if (s.mode === 'curve' || s.mode === 'custom' || s.mode === 'manual') {
      const p1 = pct(s.fan1);
      const p2 = pct(s.fan2);
      $('f1').value = p1; $('v1').textContent = p1;
      $('f2').value = p2; $('v2').textContent = p2;
    }
    refreshUI();
    const cpu = s.temps.find(t => t.name === 'k10temp');
    if (cpu) $('cpuPct').textContent = `${fmt(cpu.temp)}°${fahrenheit?'F':'C'}`;
    const gpu = s.temps.find(t => t.name === 'amdgpu');
    if (gpu) $('gpuPct').textContent = `${fmt(gpu.temp)}°${fahrenheit?'F':'C'}`;
    const grid = $('temps'); grid.innerHTML = '';
    s.temps.forEach(t=>{
      const d = document.createElement('div'); d.className='temp';
      d.innerHTML=`<div class="l">${t.name} · ${t.label}</div><div class="t">${fmt(t.temp)}°${fahrenheit?'F':'C'}</div>`;
      grid.appendChild(d);
    });
  }).catch(()=>{});
}

let refreshTimer = setInterval(update, refreshMs);
update();
</script></body></html>"""

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a, **k): pass
    def do_GET(self):
        if self.path == '/':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(HTML.encode())
        elif self.path == '/snapshot':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(json.dumps(snapshot()).encode())
    def do_POST(self):
        n = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(n)) if n else {}
        if self.path == '/set':
            f = int(body.get('fan', 0)); p = int(body.get('pct', 0))
            if f in (1, 2) and 0 <= p <= 100:
                state['targets'][f] = p
                # # ponytail: /set also pins manual mode. The poller will
                # keep writing this value at 50Hz, no drift. (Setting the
                # slider drops the user out of curve mode automatically.)
                if state['mode'] not in ('manual', 'released'):
                    state['mode'] = 'manual'
                write_duty(f, max(0, min(state['max_duty'], p * 2)))
        elif self.path == '/profile':
            p = body.get('profile', 'balanced')
            if p in ('silent', 'balanced', 'performance', 'custom'):
                state['profile'] = p
                state['mode'] = 'custom' if p == 'custom' else 'curve'
        elif self.path == '/mode':
            m = body.get('mode', 'curve')
            if m in ('curve', 'manual', 'released', 'custom'):
                state['mode'] = m
                if m == 'released':
                    release_manual()
                else:
                    lock_manual()
        elif self.path == '/custom':
            c = body.get('curve', [])
            if isinstance(c, list) and len(c) >= 2:
                # validate: list of [temp, duty] pairs, all numbers
                clean = []
                for row in c:
                    if (isinstance(row, list) and len(row) == 2
                            and all(isinstance(x, (int, float)) for x in row)):
                        clean.append([max(0, min(150, int(row[0]))), max(0, min(198, int(row[1])))])
                if len(clean) >= 2:
                    clean.sort(key=lambda r: r[0])
                    state['custom_curve'] = clean
                    state['profile'] = 'custom'
                    state['mode'] = 'custom'
        elif self.path == '/config':
            if 'max_duty' in body:
                state['max_duty'] = max(0, min(198, int(body['max_duty'])))
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

def shutdown(*_):
    try:
        if state.get('mode') != 'released':
            release_manual()
    except Exception: pass
    try: os.close(FD)
    except Exception: pass
    subprocess.run(["systemctl", "start", "fan-daemon"], capture_output=True)
    sys.exit(0)

signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)

if __name__ == '__main__':
    threading.Thread(target=lambda: http.server.HTTPServer(('127.0.0.1', 4444), Handler).serve_forever(), daemon=True).start()
    webbrowser.open('http://127.0.0.1:4444')
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt: shutdown()