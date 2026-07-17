#!/usr/bin/env python3
"""Web UI for /dev/tuxedo_io fan control. Listens on 127.0.0.1:4444."""
import ctypes, fcntl, http.server, json, os, pathlib, signal, subprocess, sys, threading, time, webbrowser

MAGIC_RD = 0xEF; MAGIC_WR = 0xF0
IOC_R, IOC_W, SZ = 2, 1, 8
def ioc(d,t,n,s): return (d<<30)|(t<<8)|(n<<0)|(s<<16)

R_FS1  = ioc(IOC_R, MAGIC_RD, 0x10, SZ)
R_FS2  = ioc(IOC_R, MAGIC_RD, 0x11, SZ)
R_TEMP = ioc(IOC_R, MAGIC_RD, 0x12, SZ)
R_TEMP2= ioc(IOC_R, MAGIC_RD, 0x13, SZ)
W_FS1  = ioc(IOC_W, MAGIC_WR, 0x10, SZ)
W_FS2  = ioc(IOC_W, MAGIC_WR, 0x11, SZ)
W_MODE = ioc(IOC_W, MAGIC_WR, 0x12, SZ)

FD = os.open('/dev/tuxedo_io', os.O_RDWR)
BUF = (ctypes.c_int64)()

def rd(cmd):
    BUF.value = 0; fcntl.ioctl(FD, cmd, BUF, True); return BUF.value & 0xFF

state = {'targets': {1: 0, 2: 0}, 'auto': False, 'manual': False}

def lock_manual():
    BUF.value = 0x40; fcntl.ioctl(FD, W_MODE, BUF, True); state['manual'] = True
def release_manual():
    BUF.value = 0; fcntl.ioctl(FD, W_MODE, BUF, True); state['manual'] = False

def sensors():
    out = []
    for h in sorted(pathlib.Path('/sys/class/hwmon').glob('hwmon*')):
        try: nm = (h/'name').read_text().strip()
        except Exception: continue
        for t in sorted(h.glob('temp*_input')):
            try: out.append({'name': nm, 'label': t.stem, 'temp': int(t.read_text().strip())/1000})
            except Exception: pass
    return out

def snapshot():
    return {
        'fan1': rd(R_FS1), 'fan2': rd(R_FS2),
        'ec_temp1': rd(R_TEMP), 'ec_temp2': rd(R_TEMP2),
        'targets': dict(state['targets']),
        'auto': state['auto'],
        'temps': sensors(),
    }

def poll():
    if state['auto']: return
    for f, cmd in [(1, W_FS1), (2, W_FS2)]:
        BUF.value = state['targets'][f]
        fcntl.ioctl(FD, cmd, BUF, True)

def poller():
    while True:
        poll(); time.sleep(0.1)

subprocess.run(["systemctl", "stop", "fan-daemon"], capture_output=True)
lock_manual()
threading.Thread(target=poller, daemon=True).start()

HTML = r"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="color-scheme" content="dark"><title>Fan Control</title>
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
input[type=range]{-webkit-appearance:none;appearance:none;width:100%;height:6px;border-radius:3px;background:#21262d;outline:none;margin-top:4px}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:18px;height:18px;border-radius:50%;background:var(--accent);cursor:pointer;border:2px solid var(--accent2);transition:.1s}
input[type=range]::-webkit-slider-thumb:hover{transform:scale(1.15)}
.bar{height:8px;border-radius:4px;background:#21262d;overflow:hidden;margin-top:8px}
.bar > div{height:100%;background:linear-gradient(90deg,var(--green),var(--orange),var(--red));transition:width .3s}
.temp-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}
.temp{background:#0d1117;border:1px solid var(--border);border-radius:8px;padding:12px}
.temp .l{color:var(--dim);font-size:11px;text-transform:uppercase}
.temp .t{font-size:22px;font-weight:600;font-variant-numeric:tabular-nums;margin-top:4px}
.btn{background:#21262d;color:var(--text);border:1px solid var(--border);border-radius:6px;padding:8px 14px;font-size:13px;cursor:pointer}
.btn:hover{background:#30363d}
.btn.warn{background:var(--red);border-color:var(--red);color:#fff}
.btn-row{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
.setting{display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-bottom:1px solid var(--border)}
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
<div class="sub"><span class="live-dot"></span>Live <span class="tag">manual mode</span></div>
<div class="group"><div class="row"><label>CPU Fan</label><span><span class="v" id="v1">0</span>% <span class="d" id="a1">(read 0)</span></span></div>
<input type="range" id="f1" min="0" max="100" value="0"><div class="bar"><div id="bar1" style="width:0%"></div></div></div>
<div class="group"><div class="row"><label>GPU Fan</label><span><span class="v" id="v2">0</span>% <span class="d" id="a2">(read 0)</span></span></div>
<input type="range" id="f2" min="0" max="100" value="0"><div class="bar"><div id="bar2" style="width:0%"></div></div></div>
<div class="group">
<h2>Presets</h2>
<div class="preset"><button data-v="0">Silent</button><button data-v="30">Low</button><button data-v="50">Med</button><button data-v="75">High</button><button data-v="99">Max</button></div>
<div class="btn-row"><button class="btn" id="link">🔗 Link fans</button><button class="btn warn" id="stop">⏹ Stop (0%)</button></div>
</div>
<div class="group"><h2>Live Sensors</h2><div class="temp-grid" id="temps"></div>
<div class="sub" style="margin-top:12px">EC: <span id="ec1">--</span>°C / <span id="ec2">--</span>°C</div></div>
</div>
<div class="card">
<h2>Settings</h2>
<div class="setting"><label>Theme</label><select id="theme"><option value="dark">Dark</option><option value="midnight">Midnight</option><option value="light">Light</option></select></div>
<div class="setting"><label>Show °F</label><input type="checkbox" id="fahr" style="width:18px;height:18px"></div>
<div class="btn-row" style="margin-top:16px"><button class="btn primary" id="restore">Release control</button></div>
</div>
</div>
<script>
const $=id=>document.getElementById(id);let linked=true,fahrenheit=false;
function fmt(t){return fahrenheit?(t*9/5+32).toFixed(1):t.toFixed(1)}
$('f1').addEventListener('input',e=>{const v=+e.target.value;$('v1').textContent=v;if(linked){$('f2').value=v;$('v2').textContent=v}setFan(1,v);if(linked)setFan(2,v)});
$('f2').addEventListener('input',e=>{const v=+e.target.value;$('v2').textContent=v;if(linked){$('f1').value=v;$('v1').textContent=v}setFan(2,v);if(linked)setFan(1,v)});
document.querySelectorAll('.preset button').forEach(b=>{b.addEventListener('click',()=>{const v=+b.dataset.v;$('f1').value=v;$('f2').value=v;$('v1').textContent=v;$('v2').textContent=v;setFan(1,v);setFan(2,v)})});
$('link').addEventListener('click',()=>{linked=!linked;$('link').textContent=linked?'🔗 Linked':'⛓ Unlinked'});
$('stop').addEventListener('click',()=>{$('f1').value=0;$('f2').value=0;$('v1').textContent=0;$('v2').textContent=0;setFan(1,0);setFan(2,0)});
$('fahr').addEventListener('change',e=>{fahrenheit=e.target.checked;update()});
function theme(t){
  const light = {'--bg':'#f6f8fa','--card':'#fff','--text':'#1f2328','--dim':'#59636e','--border':'#d1d9e0'};
  const dark = {'--bg':'#000','--card':'#0a0a0a'};
  const vars = t==='light' ? light : t==='midnight' ? dark : null;
  ['--bg','--card','--text','--dim','--border'].forEach(k=>{
    if (vars && k in vars) document.documentElement.style.setProperty(k, vars[k]);
    else document.documentElement.style.removeProperty(k);
  });
}
$('theme').addEventListener('change',e=>theme(e.target.value));
function setFan(f,v){fetch('/set',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({fan:f,pct:v})}).catch(()=>{})}
function update(){fetch('/snapshot').then(r=>r.json()).then(s=>{$('a1').textContent=`(read ${s.fan1})`;$('a2').textContent=`(read ${s.fan2})`;$('bar1').style.width=`${(s.fan1/2.55)|0}%`;$('bar2').style.width=`${(s.fan2/2.55)|0}%`;$('ec1').textContent=fmt(s.ec_temp1);$('ec2').textContent=fmt(s.ec_temp2);const grid=$('temps');grid.innerHTML='';s.temps.forEach(t=>{const d=document.createElement('div');d.className='temp';d.innerHTML=`<div class="l">${t.name} · ${t.label}</div><div class="t">${fmt(t.temp)}°${fahrenheit?'F':'C'}</div>`;grid.appendChild(d)})})}
$('restore').addEventListener('click',()=>{fetch('/restore',{method:'POST'}).then(()=>{$('f1').value=0;$('f2').value=0;$('v1').textContent=0;$('v2').textContent=0})});
update();setInterval(update,1000);
</script></body></html>"""

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a, **k): pass
    def do_GET(self):
        if self.path == '/':
            self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.end_headers(); self.wfile.write(HTML.encode())
        elif self.path == '/snapshot':
            self.send_response(200); self.send_header('Content-Type','application/json'); self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write(json.dumps(snapshot()).encode())
    def do_POST(self):
        n = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(n))
        if self.path == '/set':
            f = int(body['fan']); p = int(body['pct'])
            if f in (1,2) and 0 <= p <= 100:
                state['targets'][f] = p
                BUF.value = int(max(0, min(198, p*2)))
                fcntl.ioctl(FD, (W_FS1,W_FS2)[f-1], BUF, True)
        elif self.path == '/restore':
            state['auto'] = True; state['targets'] = {1:0,2:0}; release_manual()
        self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers(); self.wfile.write(b'{"ok":true}')

def shutdown(*_):
    try:
        if state.get('manual'): release_manual()
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