#!/usr/bin/env python3
"""Fan Control web UI — 500ms re-assert + live sensors."""
import ctypes, fcntl, http.server, json, os, pathlib, signal, subprocess, sys, threading, time, webbrowser

MAGIC_RD = 0xEF; MAGIC_WR = 0xF0
IOC_R = 2; IOC_W = 1; SZ = 8
def ioc(d,t,n,s): return (d<<30)|(t<<8)|(n<<0)|(s<<16)

R_FS1 = ioc(IOC_R, MAGIC_RD, 0x10, SZ)
R_FS2 = ioc(IOC_R, MAGIC_RD, 0x11, SZ)
R_TEMP = ioc(IOC_R, MAGIC_RD, 0x12, SZ)
R_TEMP2 = ioc(IOC_R, MAGIC_RD, 0x13, SZ)
W_FS1 = ioc(IOC_W, MAGIC_WR, 0x10, SZ)
W_FS2 = ioc(IOC_W, MAGIC_WR, 0x11, SZ)

fd = os.open('/dev/tuxedo_io', os.O_RDWR)
buf = (ctypes.c_int64)()

def read_u32(cmd):
    buf.value = 0
    fcntl.ioctl(fd, cmd, buf, True)
    return buf.value & 0xFF

def set_fan(f, pct):
    d = int(max(0, min(200, int(pct) * 2)))
    buf.value = d
    fcntl.ioctl(fd, W_FS1 if f==1 else W_FS2, buf, True)

state = {
    'targets': {1: 0, 2: 0},
    'last_duty': {1: 0, 2: 0},
    'auto': False,
    'interval_ms': 500,
    'max_duty': 200,
}

def temp_sources():
    out = []
    for h in sorted(pathlib.Path('/sys/class/hwmon').glob('hwmon*')):
        try:
            name = (h / 'name').read_text().strip()
        except Exception:
            continue
        for t in sorted(h.glob('temp*_input')):
            try:
                v = int(t.read_text().strip()) / 1000
            except Exception:
                continue
            out.append({'hwmon': h.name, 'name': name, 'label': t.stem, 'temp': v})
    return out

def snapshot():
    return {
        'fan1': read_u32(R_FS1),
        'fan2': read_u32(R_FS2),
        'ec_temp1': read_u32(R_TEMP),
        'ec_temp2': read_u32(R_TEMP2),
        'targets': dict(state['targets']),
        'auto': state['auto'],
        'temps': temp_sources(),
    }

def poll():
    s = state
    if s['auto']:
        return
    for f in (1, 2):
        t = s['targets'][f]
        if t == 0 and s['last_duty'][f] == 0:
            continue
        set_fan(f, t)
        s['last_duty'][f] = t

def poller():
    while True:
        poll()
        time.sleep(state['interval_ms'] / 1000)

subprocess.run(["systemctl", "stop", "tcc-fan"], capture_output=True)
threading.Thread(target=poller, daemon=True).start()

HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="color-scheme" content="dark">
<title>Fan Control</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#0d1117;--card:#161b22;--border:#30363d;--text:#e6edf3;--dim:#8b949e;--accent:#58a6ff;--accent2:#1f6feb;--green:#3fb950;--orange:#d29922;--red:#f85149}
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
.group:last-child{margin-bottom:0}
input[type=range]{-webkit-appearance:none;appearance:none;width:100%;height:6px;border-radius:3px;background:#21262d;outline:none;margin-top:4px}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:18px;height:18px;border-radius:50%;background:var(--accent);cursor:pointer;border:2px solid var(--accent2);transition:.1s}
input[type=range]::-webkit-slider-thumb:hover{transform:scale(1.15)}
input[type=range]::-moz-range-thumb{width:18px;height:18px;border-radius:50%;background:var(--accent);cursor:pointer;border:2px solid var(--accent2)}
.bar{height:8px;border-radius:4px;background:#21262d;overflow:hidden;margin-top:8px}
.bar > div{height:100%;background:linear-gradient(90deg,var(--green),var(--orange),var(--red));transition:width .3s}
.temp-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}
.temp{background:#0d1117;border:1px solid var(--border);border-radius:8px;padding:12px}
.temp .l{color:var(--dim);font-size:11px;text-transform:uppercase}
.temp .t{font-size:22px;font-weight:600;font-variant-numeric:tabular-nums;margin-top:4px}
.btn{background:#21262d;color:var(--text);border:1px solid var(--border);border-radius:6px;padding:8px 14px;font-size:13px;cursor:pointer;transition:.1s}
.btn:hover{background:#30363d}
.btn.primary{background:var(--accent2);border-color:var(--accent2);color:#fff}
.btn.primary:hover{background:var(--accent)}
.btn.warn{background:var(--red);border-color:var(--red);color:#fff}
.btn-row{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
.setting{display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-bottom:1px solid var(--border)}
.setting:last-child{border-bottom:none}
.setting label{font-size:13px;color:var(--dim)}
.setting input[type=number]{width:80px;background:#0d1117;border:1px solid var(--border);color:var(--text);padding:4px 8px;border-radius:4px;font-family:inherit;font-size:13px;text-align:right}
.setting select{background:#0d1117;border:1px solid var(--border);color:var(--text);padding:4px 8px;border-radius:4px;font-family:inherit;font-size:13px}
.preset{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
.preset button{flex:1;min-width:60px;background:#21262d;color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:12px;cursor:pointer}
.preset button:hover{background:#30363d}
.live-dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--green);margin-right:6px;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.tag{display:inline-block;background:#21262d;border:1px solid var(--border);border-radius:4px;padding:2px 6px;font-size:11px;color:var(--dim);margin-left:8px}
</style></head><body>
<div class="wrap">
<div class="card">
<h1>⚡ Fan Control</h1>
<div class="sub"><span class="live-dot"></span>Live <span class="tag">re-asserting every <span id="ivTxt">500</span>ms</span></div>

<div class="group">
<div class="row"><label>CPU Fan</label><span><span class="v" id="v1">0</span>% <span class="d" id="a1">(read 0)</span></span></div>
<input type="range" id="f1" min="0" max="100" value="0">
<div class="bar"><div id="bar1" style="width:0%"></div></div>
</div>

<div class="group">
<div class="row"><label>GPU Fan</label><span><span class="v" id="v2">0</span>% <span class="d" id="a2">(read 0)</span></span></div>
<input type="range" id="f2" min="0" max="100" value="0">
<div class="bar"><div id="bar2" style="width:0%"></div></div>
</div>

<div class="group">
<h2>Presets</h2>
<div class="preset" id="presets">
<button data-v="0">Silent</button>
<button data-v="30">Low</button>
<button data-v="50">Med</button>
<button data-v="75">High</button>
<button data-v="100">Max</button>
</div>
<div class="btn-row">
<button class="btn" id="link">🔗 Link fans</button>
<button class="btn warn" id="stop">⏹ Stop (0%)</button>
</div>
</div>

<div class="group">
<h2>Live Sensors</h2>
<div class="temp-grid" id="temps"></div>
<div class="sub" style="margin-top:12px">EC: <span id="ec1">--</span>°C / <span id="ec2">--</span>°C</div>
</div>
</div>

<div class="card">
<h2>Settings</h2>
<div class="setting">
<label>Re-assert interval (ms)</label>
<input type="number" id="interval" min="100" max="5000" step="100" value="500">
</div>
<div class="setting">
<label>Max duty cap</label>
<input type="number" id="maxduty" min="100" max="255" step="1" value="200">
</div>
<div class="setting">
<label>Auto (release control)</label>
<input type="checkbox" id="auto" style="width:18px;height:18px">
</div>
<div class="setting">
<label>Theme</label>
<select id="theme"><option value="dark">Dark</option><option value="midnight">Midnight</option><option value="light">Light</option></select>
</div>
<div class="setting">
<label>Show °F</label>
<input type="checkbox" id="fahr" style="width:18px;height:18px">
</div>
<div class="btn-row" style="margin-top:16px">
<button class="btn" id="apply">Apply settings</button>
<button class="btn primary" id="restore">Release control</button>
</div>
</div>
</div>

<script>
const $=id=>document.getElementById(id);
let linked=true, fahrenheit=false;

function fmt(t){return fahrenheit?(t*9/5+32).toFixed(1):t.toFixed(1)}

$('f1').addEventListener('input',e=>{const v=+e.target.value;$('v1').textContent=v;if(linked){$('f2').value=v;$('v2').textContent=v}setFan(1,v);if(linked)setFan(2,v)});
$('f2').addEventListener('input',e=>{const v=+e.target.value;$('v2').textContent=v;if(linked){$('f1').value=v;$('v1').textContent=v}setFan(2,v);if(linked)setFan(1,v)});

document.querySelectorAll('.preset button').forEach(b=>{
b.addEventListener('click',()=>{
const v=+b.dataset.v;
$('f1').value=v;$('f2').value=v;$('v1').textContent=v;$('v2').textContent=v;
setFan(1,v);setFan(2,v);
})});

$('link').addEventListener('click',()=>{linked=!linked;$('link').textContent=linked?'🔗 Linked':'⛓ Unlinked'});
$('stop').addEventListener('click',()=>{$('f1').value=0;$('f2').value=0;$('v1').textContent=0;$('v2').textContent=0;setFan(1,0);setFan(2,0)});

$('fahr').addEventListener('change',e=>{fahrenheit=e.target.checked;updateTemps()});

$('theme').addEventListener('change',e=>{
const t=e.target.value;
if(t==='midnight'){document.documentElement.style.setProperty('--bg','#000');document.documentElement.style.setProperty('--card','#0a0a0a')}
else if(t==='light'){document.documentElement.style.setProperty('--bg','#f6f8fa');document.documentElement.style.setProperty('--card','#fff');document.documentElement.style.setProperty('--text','#1f2328');document.documentElement.style.setProperty('--dim','#59636e');document.documentElement.style.setProperty('--border','#d1d9e0')}
else{['--bg','--card','--text','--dim','--border'].forEach(k=>document.documentElement.style.removeProperty(k))}
});

function setFan(f,v){fetch('/set',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({fan:f,pct:v})}).catch(()=>{})}

$('apply').addEventListener('click',()=>{
fetch('/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
interval:+$('interval').value,
max_duty:+$('maxduty').value
})}).then(()=>{$('ivTxt').textContent=$('interval').value})});

$('restore').addEventListener('click',()=>{
fetch('/restore',{method:'POST'}).then(()=>{$('f1').value=0;$('f2').value=0;$('v1').textContent=0;$('v2').textContent=0;$('auto').checked=false})});

let prevAuto=false;
function update(){
fetch('/snapshot').then(r=>r.json()).then(s=>{
$('a1').textContent=`(read ${s.fan1})`;$('a2').textContent=`(read ${s.fan2})`;
$('bar1').style.width=`${(s.fan1/2.55)|0}%`;$('bar2').style.width=`${(s.fan2/2.55)|0}%`;
$('ec1').textContent=fmt(s.ec_temp1);$('ec2').textContent=fmt(s.ec_temp2);
const grid=$('temps');grid.innerHTML='';
s.temps.forEach(t=>{const d=document.createElement('div');d.className='temp';d.innerHTML=`<div class="l">${t.name} · ${t.label}</div><div class="t">${fmt(t.temp)}°${fahrenheit?'F':'C'}</div>`;grid.appendChild(d)});
if(s.auto!==prevAuto){prevAuto=s.auto;$('auto').checked=s.auto}
})}
update();setInterval(update,1000);
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
        body = json.loads(self.rfile.read(n))
        if self.path == '/set':
            f = int(body['fan'])
            p = int(body['pct'])
            if f in (1, 2) and 0 <= p <= 100:
                state['targets'][f] = p
                set_fan(f, p)
                state['last_duty'][f] = p
        elif self.path == '/config':
            if 'interval' in body:
                iv = max(100, min(5000, int(body['interval'])))
                state['interval_ms'] = iv
            if 'max_duty' in body:
                state['max_duty'] = max(100, min(255, int(body['max_duty'])))
        elif self.path == '/restore':
            state['auto'] = True
            state['targets'] = {1: 0, 2: 0}
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

def shutdown(*_):
    try: os.close(fd)
    except Exception: pass
    subprocess.run(["systemctl", "start", "tcc-fan"], capture_output=True)
    sys.exit(0)

signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)

if __name__ == '__main__':
    threading.Thread(target=lambda: http.server.HTTPServer(('127.0.0.1', 4444), Handler).serve_forever(), daemon=True).start()
    webbrowser.open('http://127.0.0.1:4444')
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt: shutdown()