import time, os, json, threading, glob, subprocess, serial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from ssd1306 import SSD1306

BUS, ADDR = 0, 0x3C
REFRESH = 1.0

SNAP = {
    "wifi_pct": 0, "wifi_dbm": 0, "cpu_pct": 0.0, "load1": 0.0,
    "temp_c": 0.0, "ram_used_mb": 0, "ram_total_mb": 0, "ram_pct": 0.0,
    "uptime_s": 0,
    "weight_kg": 0.0, "ch": [0.0, 0.0, 0.0, 0.0],
    "overload": False, "scale_conn": False, "scale_port": "",
    "cal": [0.0, 0.0, 0.0, 0.0],
    "taresync": "", "taresync_t": 0,
    "tare_locked": False,
    "log_count": 0, "last_log": "", "log_flash": 0.0, "last_log_w": 0.0,
}

LOG_FILE = "/opt/scale/weights.csv"
LOG_MIN_KG = 20.0
LOG_SETTLE_S = 2.0          # weight must stay stable this long before logging
LOG_SETTLE_TOL = 0.3        # kg tolerance for "stable"
OLED_OFF_S = 40.0           # turn OLED off after this long showing a steady value
LOG_LOCK = threading.Lock()
LOG_STATE = {"stable_since": 0.0, "last_val": None, "done": False, "logged_w": 0.0, "logged_at": 0.0}

DBG = "/tmp/scale_debug.log"
def dbg(msg):
    try:
        with open(DBG, "a") as f:
            f.write("%s %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass
SNAP_LOCK = threading.Lock()
ACT_LOCK = threading.Lock()

def _read(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return default

def wifi_status():
    try:
        with open("/proc/net/wireless") as f:
            for line in f:
                if "wlan0" in line:
                    p = line.split()
                    link = int(p[2].rstrip(".")); level = int(p[3].rstrip("."))
                    return max(0, min(100, int(link/70*100))), level
    except Exception:
        pass
    return 0, 0

def load_avg():
    return float(_read("/proc/loadavg", "0 0 0").split()[0])

def cpu_temp():
    return int(_read("/sys/class/thermal/thermal_zone0/temp", "0")) / 1000.0

def cpu_load():
    def read():
        with open("/proc/stat") as f:
            p = f.readline().split()
        idle = int(p[4]) + int(p[5]); total = sum(int(x) for x in p[1:8])
        return idle, total
    i0, t0 = read(); time.sleep(0.2); i1, t1 = read()
    dt = t1 - t0
    if dt == 0: return 0.0
    return max(0.0, min(100.0, (1 - (i1-i0)/dt) * 100.0))

def mem_usage():
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1); info[k.strip()] = int(v.split()[0])
    total = info["MemTotal"]; used = total - info["MemAvailable"]
    return used//1024, total//1024, used/total*100.0

def uptime():
    return int(float(_read("/proc/uptime", "0").split()[0]))

def find_port():
    # stable symlink first, then fall back to any usb serial
    if os.path.exists("/dev/scale"):
        return "/dev/scale"
    for p in glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"):
        return p
    return None

ser = None
def scale_reader():
    global ser
    auto_tare_done = False
    step_tare_state = "idle"   # idle -> window -> done
    step_tare_wait = 0.0
    step_tare_tared_at = 0.0
    step_tare_locked = False   # armed-off until weight leaves the scale
    while True:
        port = find_port()
        if port is None:
            with SNAP_LOCK:
                SNAP["scale_conn"] = False; SNAP["scale_port"] = ""
            auto_tare_done = False
            step_tare_state = "idle"
            step_tare_locked = False
            with SNAP_LOCK: SNAP["taresync"] = ""
            time.sleep(2); continue
        try:
            if ser is None or not ser.is_open:
                ser = serial.Serial(port, 115200, timeout=1)
                ser.reset_input_buffer()
                auto_tare_done = False
                step_tare_state = "idle"
                dbg("serial reopen")
            with SNAP_LOCK:
                SNAP["scale_conn"] = True; SNAP["scale_port"] = port
            if not auto_tare_done:
                time.sleep(2)
                scale_cmd("t")
                auto_tare_done = True
            line = ser.readline().decode("ascii", "ignore").strip()
            if not line.endswith("$"): continue
            body = line[:-1]
            if "|" in body and body.replace("|", "").replace("-", "").isdigit():
                v = [int(x) for x in body.split("|")]
                if len(v) == 4:
                    ch = [x/100.0 for x in v]
                    with SNAP_LOCK:
                        SNAP["ch"] = ch
                        SNAP["weight_kg"] = round(sum(ch), 3)
                    # step-on auto tare: when weight first exceeds 10 kg a
                    # 5 s window opens. If weight drops below 5 kg within
                    # that window -> tare. If still on after 5 s -> cancel.
                    TARE_WINDOW = 5.0
                    TARE_GRACE = 5.0
                    now = time.time()
                    if auto_tare_done and step_tare_state == "idle":
                        if not step_tare_locked and SNAP["weight_kg"] > 10.0:
                            step_tare_state = "window"
                            step_tare_wait = now
                            dbg("->window w=%.2f" % SNAP["weight_kg"])
                            with SNAP_LOCK:
                                SNAP["taresync"] = "STEP ON"
                                SNAP["taresync_t"] = TARE_WINDOW
                    elif step_tare_state == "window":
                        rem = max(0, TARE_WINDOW - (now - step_tare_wait))
                        with SNAP_LOCK:
                            SNAP["taresync_t"] = rem
                        if SNAP["weight_kg"] < 5.0:
                            scale_cmd("t")
                            step_tare_state = "idle"
                            step_tare_tared_at = now
                            with SNAP_LOCK:
                                SNAP["taresync"] = "TARED"
                                SNAP["taresync_t"] = 0
                        elif now - step_tare_wait >= TARE_WINDOW:
                            step_tare_state = "grace"
                            step_tare_wait = now
                            dbg("->grace w=%.2f" % SNAP["weight_kg"])
                            with SNAP_LOCK:
                                SNAP["taresync"] = "TIMEOUT"
                                SNAP["taresync_t"] = TARE_GRACE
                    elif step_tare_state == "grace":
                        rem = max(0, TARE_GRACE - (now - step_tare_wait))
                        with SNAP_LOCK:
                            SNAP["taresync_t"] = rem
                        if SNAP["weight_kg"] < 5.0:
                            scale_cmd("t")
                            step_tare_state = "idle"
                            step_tare_tared_at = now
                            with SNAP_LOCK:
                                SNAP["taresync"] = "TARED"
                                SNAP["taresync_t"] = 0
                        elif now - step_tare_wait >= TARE_GRACE:
                            step_tare_state = "idle"
                            step_tare_locked = True
                            dbg("->LOCKED w=%.2f" % SNAP["weight_kg"])
                            with SNAP_LOCK:
                                SNAP["taresync"] = "TIMEOUT"
                                SNAP["taresync_t"] = 0
                    # clear the lock once the scale is empty again
                    if step_tare_locked and SNAP["weight_kg"] < 5.0:
                        step_tare_locked = False
                        with SNAP_LOCK:
                            SNAP["taresync"] = ""
                            SNAP["tare_locked"] = False
                    elif not step_tare_locked and SNAP["tare_locked"]:
                        with SNAP_LOCK: SNAP["tare_locked"] = False
                    if step_tare_locked and not SNAP["tare_locked"]:
                        with SNAP_LOCK: SNAP["tare_locked"] = True
                    if step_tare_state == "idle" and step_tare_tared_at:
                        if now - step_tare_tared_at > 3.0:
                            step_tare_tared_at = 0.0
                            with SNAP_LOCK:
                                SNAP["taresync"] = ""
                                SNAP["taresync_t"] = 0
                    # --- logging: only after timeout lockout, >min kg, settled,
                    #     exactly ONE entry per step-on session ---
                    w = SNAP["weight_kg"]
                    if step_tare_locked and w > LOG_MIN_KG:
                        if LOG_STATE["last_val"] is None or abs(w - LOG_STATE["last_val"]) > LOG_SETTLE_TOL:
                            LOG_STATE["last_val"] = w
                            LOG_STATE["stable_since"] = now
                        elif not LOG_STATE["done"] and now - LOG_STATE["stable_since"] >= LOG_SETTLE_S:
                            log_weight(w, ch)
                            dbg("LOG w=%.2f" % w)
                            LOG_STATE["done"] = True
                            LOG_STATE["logged_w"] = w
                            LOG_STATE["logged_at"] = now
                    else:
                        LOG_STATE["last_val"] = None
                        LOG_STATE["stable_since"] = 0.0
                        LOG_STATE["done"] = False
            elif body.startswith("OVL:"):
                parts = body[4:].split("|")
                with SNAP_LOCK:
                    SNAP["overload"] = any(x == "1" for x in parts[:4])
            elif body.startswith("CAL:"):
                parts = body[4:].split("|")
                if len(parts) == 4 and all(p.replace(".","").replace("-","").isdigit() for p in parts):
                    with SNAP_LOCK:
                        SNAP["cal"] = [float(x)/10.0 for x in parts]
        except Exception:
            try:
                if ser: ser.close()
            except Exception: pass
            ser = None
            with SNAP_LOCK: SNAP["scale_conn"] = False
            time.sleep(2)

def scale_cmd(cmd):
    global ser
    with SNAP_LOCK:
        if ser and ser.is_open:
            try:
                ser.write((cmd + "\n").encode()); return True
            except Exception:
                return False
    return False

def log_weight(weight_kg, ch):
    # only call once a settle + post-timeout + >min condition is confirmed
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = "%s,%.3f\n" % (ts, weight_kg)
    try:
        d = os.path.dirname(LOG_FILE)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        with LOG_LOCK:
            with open(LOG_FILE, "a") as f:
                if f.tell() == 0:
                    f.write("timestamp,total_kg\n")
                f.write(line)
        with SNAP_LOCK:
            SNAP["log_count"] += 1
            SNAP["last_log"] = ts
            SNAP["last_log_w"] = weight_kg
            SNAP["log_flash"] = time.time()
    except Exception as e:
        dbg("LOG ERR %s" % e)

def collect():
    while True:
        wp, wl = wifi_status()
        c = cpu_load()
        la = load_avg()
        t = cpu_temp()
        mu, mt, mp = mem_usage()
        with SNAP_LOCK:
            SNAP["wifi_pct"] = wp; SNAP["wifi_dbm"] = wl
            SNAP["cpu_pct"] = round(c, 1); SNAP["load1"] = round(la, 2)
            SNAP["temp_c"] = round(t, 1); SNAP["ram_used_mb"] = mu
            SNAP["ram_total_mb"] = mt; SNAP["ram_pct"] = round(mp, 1)
            SNAP["uptime_s"] = uptime()
        time.sleep(REFRESH)

d = SSD1306(bus=BUS, addr=ADDR)
d.init()

def draw():
    # big weight only, at ~10 Hz (matches HX711 10 SPS), as large as fits
    oled_on = True
    last_val = None
    last_change = time.time()
    d.power(True)
    while True:
        with SNAP_LOCK:
            s = dict(SNAP)
        w = s["weight_kg"]
        # detect a "sensed" change: weight differs from last shown value,
        # or any weight is present after being empty
        if last_val is None or abs(w - last_val) > 0.05:
            last_val = w
            last_change = time.time()
            if not oled_on:
                d.power(True); oled_on = True
        elif time.time() - last_change > OLED_OFF_S and oled_on and s["scale_conn"]:
            d.power(False); oled_on = False
        if not oled_on:
            time.sleep(0.5); continue
        d.clear()
        if s["scale_conn"]:
            w = s["weight_kg"]
            if time.time() - s["log_flash"] < 3.0:
                # full LOGGED confirmation screen
                txt = "%.1f" % s["last_log_w"]
                sc = 5
                while sc > 1 and len(txt) * (5*sc + 2) > 126:
                    sc -= 1
                x = max(0, (128 - len(txt)*(5*sc + 2)) // 2)
                d.text_big(txt, x, 1, sc)
                lbl = "LOGGED"
                lw = len(lbl) * (5*2 + 1) - 1
                d.text(lbl, max(0, (128 - lw)//2), 48, 2)
            else:
                txt = "%.1f" % w                 # e.g. "12.3" or "100.0"
                # largest scale where the string fits 128px wide (covers up to 999.9)
                sc = 5
                while sc > 1 and len(txt) * (5*sc + 2) > 126:
                    sc -= 1
                x = max(0, (128 - len(txt)*(5*sc + 2)) // 2)
                y = max(0, (64 - 7*sc) // 2)
                d.text_big(txt, x, y, sc)
                if s["taresync"]:
                    d.text(s["taresync"], 0, 56, 1)
                    if s["taresync_t"] > 0:
                        d.text("%d" % int(s["taresync_t"] + 0.999), 110, 56, 1)
        else:
            d.text_big("--.-", 18, (64 - 21)//2, 3)
        d.show()
        time.sleep(0.1)

PAGE = """<!doctype html><html><head><meta charset=utf-8>
<title>Smart Scale</title><meta name=viewport content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0b0f14;--card:#151c24;--txt:#e6f0f5;--grn:#39d98a;--amb:#f5a623;--red:#ff5c5c;--blu:#3aa0ff}
*{box-sizing:border-box}body{margin:0;font-family:system-ui,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--txt)}
header{padding:1rem 1.5rem;display:flex;justify-content:space-between;align-items:center;background:linear-gradient(90deg,#151c24,#0b0f14);border-bottom:1px solid #222}
header h1{font-size:1.2rem;margin:0} .badge{font-size:.8rem;padding:.25rem .6rem;border-radius:99px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:1rem;padding:1.5rem}
.card{background:var(--card);border-radius:14px;padding:1rem 1.2rem;box-shadow:0 2px 10px rgba(0,0,0,.35)}
.card h3{margin:0 0 .5rem;font-size:.8rem;letter-spacing:.05em;text-transform:uppercase;color:#8aa0b0}
.val{font-size:2rem;font-weight:700} .sub{font-size:.85rem;color:#8aa0b0;margin-top:.3rem}
.unit{font-size:1rem;color:#8aa0b0;font-weight:400}
.chips{display:flex;gap:.5rem;flex-wrap:wrap;margin-top:.5rem}
.chip{background:#0b0f14;border:1px solid #2a3540;border-radius:8px;padding:.3rem .6rem;font-size:.8rem}
.btns{display:flex;gap:.75rem;flex-wrap:wrap;padding:0 1.5rem 1.5rem}
button{background:#1d2730;color:var(--txt);border:1px solid #2a3540;border-radius:10px;padding:.7rem 1.1rem;font-size:.95rem;cursor:pointer;transition:.15s}
button:hover{background:#26333f}
button.warn{border-color:var(--amb);color:var(--amb)}
button.danger{border-color:var(--red);color:var(--red)}
a.btn{background:#1d2730;color:var(--blu);border:1px solid #2a3540;border-radius:10px;padding:.7rem 1.1rem;font-size:.95rem;text-decoration:none}
a.btn:hover{background:#26333f}
#toast{position:fixed;bottom:1rem;left:50%;transform:translateX(-50%);background:#1d2730;border:1px solid #2a3540;padding:.7rem 1.2rem;border-radius:10px;opacity:0;transition:.3s;pointer-events:none}
#toast.show{opacity:1}
.ok{color:var(--grn)} .bad{color:var(--red)} .amb{color:var(--amb)}
</style></head><body>
<header><h1>Smart Scale</h1><span id=link class=badge>connecting…</span></header>
<div class=grid>
  <div class=card><h3>Total Weight</h3><div class=val><span id=w>--</span><span class=unit> kg</span></div>
    <div class=sub id=ovl>—</div>
    <div class=sub id=logarm>log: armed</div></div>
  <div class=card><h3>Channels</h3>
    <div class=chips><span class=chip>C1 <b id=c1>--</b></span><span class=chip>C2 <b id=c2>--</b></span>
    <span class=chip>C3 <b id=c3>--</b></span><span class=chip>C4 <b id=c4>--</b></span></div>
    <div class=sub id=sport>scale: —</div></div>
  <div class=card><h3>WiFi</h3><div class=val><span id=wp>--</span><span class=unit>%</span></div>
    <div class=sub id=wl>— dBm</div></div>
  <div class=card><h3>CPU Load</h3><div class=val><span id=cp>--</span><span class=unit>%</span></div>
    <div class=sub id=la>load: —</div></div>
  <div class=card><h3>SoC Temp</h3><div class=val><span id=tp>--</span><span class=unit> °C</span></div></div>
  <div class=card><h3>RAM</h3><div class=val><span id=rm>--</span><span class=unit>%</span></div>
    <div class=sub id=rmd>— / — MB</div></div>
  <div class=card><h3>Uptime</h3><div class=val id=up>--</div><div class=sub>seconds</div></div>
  <div class=card><h3>Weight Log</h3><div class=val><span id=lcount>0</span><span class=unit> entries</span></div>
    <div class=sub id=llast>last: —</div></div>
</div>
<div class=btns>
  <button onclick="act('tare')">Tare Scale</button>
  <a class=btn href="/log" style="background:#1d2730;color:var(--blu);border:1px solid #2a3540;border-radius:10px;padding:.7rem 1.1rem;font-size:.95rem;text-decoration:none">Weight Log</a>
  <button class=warn onclick="act('restart_prog')">Restart Program</button>
  <button class=warn onclick="act('restart')">Restart SBC</button>
  <button class=danger onclick="act('shutdown')">Shutdown SBC</button>
</div>
<div class=card style="margin:0 1.5rem 1.5rem">
  <h3>Calibration (known mass)</h3>
  <div class=chips>
    <span class=chip>Ch <select id=calch><option>0</option><option>1</option><option>2</option><option>3</option></select></span>
    <span class=chip>Mass(kg) <input id=calmass type=number step=0.01 value=1.0 style="width:5em;background:#0b0f14;color:#e6f0f5;border:1px solid #2a3540;border-radius:6px;padding:.2rem"></span>
    <button onclick="cal()">Calibrate</button>
  </div>
  <div class=sub>Place the known mass on the channel, then click Calibrate.</div>
  <div class=chips><button onclick="act('readcal')">Read from scale</button>
    <span class=chip>C1 <b id=cal0>--</b></span><span class=chip>C2 <b id=cal1>--</b></span>
    <span class=chip>C3 <b id=cal2>--</b></span><span class=chip>C4 <b id=cal3>--</b></span></div>
  <div class=sub>Saved factors persist in Arduino EEPROM across reboots.</div>
  <div class=chips style="margin-top:.6rem">
    <span class=chip>Ch <select id=setch><option>0</option><option>1</option><option>2</option><option>3</option></select></span>
    <span class=chip>Factor <input id=setval type=number step=0.1 value=14000 style="width:6em;background:#0b0f14;color:#e6f0f5;border:1px solid #2a3540;border-radius:6px;padding:.2rem"></span>
    <button onclick="setcal()">Set factor</button>
  </div>
  <div class=sub>Directly write a calibration factor (float, e.g. 14000) — same as the firmware <code>v&lt;ch&gt;:&lt;value&gt;</code> command.</div>
</div>
<div id=toast></div>
<script>
function fmt(n){return (n===null||n===undefined)?'--':Number(n).toFixed(2)}
async function f(){
 try{
  const r=await fetch('/api');const d=await r.json();
  document.getElementById('w').textContent=fmt(d.weight_kg);
  document.getElementById('c1').textContent=fmt(d.ch[0]);
  document.getElementById('c2').textContent=fmt(d.ch[1]);
  document.getElementById('c3').textContent=fmt(d.ch[2]);
  document.getElementById('c4').textContent=fmt(d.ch[3]);
  document.getElementById('wp').textContent=d.wifi_pct;
  document.getElementById('wl').textContent=d.wifi_dbm+' dBm';
  document.getElementById('cp').textContent=d.cpu_pct;
  document.getElementById('la').textContent='load: '+d.load1;
  document.getElementById('tp').textContent=d.temp_c;
  document.getElementById('rm').textContent=d.ram_pct;
  document.getElementById('rmd').textContent=d.ram_used_mb+' / '+d.ram_total_mb+' MB';
  document.getElementById('up').textContent=d.uptime_s;
  for(let i=0;i<4;i++){const e=document.getElementById('cal'+i);if(e)e.textContent=d.cal?fmt(d.cal[i]):'--';}
  document.getElementById('lcount').textContent=d.log_count;
  document.getElementById('llast').textContent='last: '+(d.last_log||'—');
  const lk=document.getElementById('link');
  if(d.scale_conn){lk.textContent='scale linked';lk.className='badge ok';document.getElementById('sport').textContent='port: '+d.scale_port;}
  else{lk.textContent='scale offline';lk.className='badge bad';document.getElementById('sport').textContent='scale: no link';}
  const o=document.getElementById('ovl');
  o.textContent=d.overload?'OVERLOAD detected':'in range';
  o.className='sub '+(d.overload?'amb':'ok');
  const la=document.getElementById('logarm');
  la.textContent='log: '+(d.tare_locked?'ARMED >20kg':'idle');
  la.className='sub '+(d.tare_locked?'amb':'ok');
 }catch(e){}
}
async function act(c){
  const r=await fetch('/api/action?cmd='+c);const j=await r.json();
  toast(j.msg||c); if(c==='restart'||c==='shutdown')toast('SBC powering…');
}
function toast(m){const t=document.getElementById('toast');t.textContent=m;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),2500);}
async function cal(){
  const ch=document.getElementById('calch').value;
  const mass=document.getElementById('calmass').value;
  const r=await fetch('/api/action?cmd=cal&ch='+ch+'&mass='+mass);const j=await r.json();
  toast(j.msg||'cal');
}
async function setcal(){
  const ch=document.getElementById('setch').value;
  const val=document.getElementById('setval').value;
  const r=await fetch('/api/action?cmd=setcal&ch='+ch+'&val='+val);const j=await r.json();
  toast(j.msg||'setcal');
}
f();setInterval(f,1000);
</script></body></html>"""

LOG_PAGE = """<!doctype html><html><head><meta charset=utf-8>
<title>Weight Log</title><meta name=viewport content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0b0f14;--card:#151c24;--txt:#e6f0f5;--grn:#39d98a;--blu:#3aa0ff}
*{box-sizing:border-box}body{margin:0;font-family:system-ui,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--txt)}
header{padding:1rem 1.5rem;display:flex;justify-content:space-between;align-items:center;background:linear-gradient(90deg,#151c24,#0b0f14);border-bottom:1px solid #222}
header h1{font-size:1.2rem;margin:0}
.btns{display:flex;gap:.75rem;flex-wrap:wrap;padding:1.5rem}
button{background:#1d2730;color:var(--txt);border:1px solid #2a3540;border-radius:10px;padding:.7rem 1.1rem;font-size:.95rem;cursor:pointer;transition:.15s}
button:hover{background:#26333f}
a.btn{background:#1d2730;color:var(--blu);border:1px solid #2a3540;border-radius:10px;padding:.7rem 1.1rem;font-size:.95rem;text-decoration:none}
a.btn:hover{background:#26333f}
table{width:100%;border-collapse:collapse;margin:0 1.5rem 1.5rem;max-width:920px}
th,td{text-align:left;padding:.5rem .8rem;border-bottom:1px solid #222;font-size:.9rem}
th{color:#8aa0b0;text-transform:uppercase;font-size:.75rem;letter-spacing:.05em}
.sub{padding:0 1.5rem 1rem;color:#8aa0b0}
#toast{position:fixed;bottom:1rem;left:50%;transform:translateX(-50%);background:#1d2730;border:1px solid #2a3540;padding:.7rem 1.2rem;border-radius:10px;opacity:0;transition:.3s;pointer-events:none}
#toast.show{opacity:1}
</style></head><body>
<header><h1>Weight Log</h1><a class=btn href="/">&larr; Dashboard</a></header>
<div class=btns>
  <a class=btn href="/log.csv">Download CSV</a>
  <button onclick="clearLog()">Clear Log</button>
  <span class=sub id=count></span>
</div>
<div class=sub>Logs only after timeout, settled, and over 20 kg.</div>
<table><thead><tr><th>Timestamp</th><th>Total (kg)</th></tr></thead>
<tbody id=rows></tbody></table>
<div id=toast></div>
<script>
async function load(){
 try{
  const r=await fetch('/api/log');const d=await r.json();
  document.getElementById('count').textContent=d.count+' entries';
  const tb=document.getElementById('rows');tb.innerHTML='';
  d.rows.slice(1).reverse().forEach(row=>{
   const c=row.split(',');if(c.length<2)return;
   const tr=document.createElement('tr');
   tr.innerHTML='<td>'+c[0]+'</td><td>'+c[1]+'</td>';
   tb.appendChild(tr);
  });
 }catch(e){}
}
function toast(m){const t=document.getElementById('toast');t.textContent=m;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),2500);}
async function clearLog(){const r=await fetch('/api/action?cmd=clear_log');const j=await r.json();toast(j.msg||'cleared');load();}
load();setInterval(load,2000);
</script></body></html>"""

class H(BaseHTTPRequestHandler):
    def _send(self, body, ctype="application/json"):
        if isinstance(body, str): body = body.encode()
        self.send_response(200); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api":
            with SNAP_LOCK:
                self._send(json.dumps(SNAP))
        elif u.path == "/api/action":
            qs = parse_qs(u.query)
            cmd = qs.get("cmd", [""])[0]
            msg = "unknown"
            with ACT_LOCK:
                if cmd == "tare":
                    ok = scale_cmd("t")
                    msg = "tare sent" if ok else "scale not connected"
                elif cmd == "cal":
                    ch = qs.get("ch", ["0"])[0]
                    mass = qs.get("mass", ["0"])[0]
                    ok = scale_cmd("k%s:%s" % (ch, mass))
                    msg = ("calibration sent ch%s %s" % (ch, mass)) if ok else "scale not connected"
                elif cmd == "readcal":
                    ok = scale_cmd("c")
                    msg = "calibration read requested" if ok else "scale not connected"
                elif cmd == "setcal":
                    ch = qs.get("ch", ["0"])[0]
                    val = qs.get("val", ["0"])[0]
                    ok = scale_cmd("v%s:%s" % (ch, val))
                    msg = ("cal factor set ch%s=%s" % (ch, val)) if ok else "scale not connected"
                elif cmd == "restart_prog":
                    subprocess.Popen(["sudo","-n","systemctl","restart","scale-status"])
                    msg = "program restarting"
                elif cmd == "restart":
                    subprocess.Popen(["sudo","-n","systemctl","reboot"])
                    msg = "SBC restarting"
                elif cmd == "shutdown":
                    subprocess.Popen(["sudo","-n","systemctl","poweroff"])
                    msg = "SBC shutting down"
                elif cmd == "clear_log":
                    with LOG_LOCK:
                        try:
                            open(LOG_FILE, "w").close()
                            with SNAP_LOCK:
                                SNAP["log_count"] = 0; SNAP["last_log"] = ""
                            msg = "log cleared"
                        except Exception:
                            msg = "clear failed"
            self._send(json.dumps({"msg": msg}))
        elif u.path == "/log.csv":
            try:
                with open(LOG_FILE, "rb") as f:
                    data = f.read()
            except Exception:
                data = b"timestamp,total_kg\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/csv")
            self.send_header("Content-Disposition",
                              "attachment; filename=weights.csv")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif u.path == "/api/log":
            try:
                with LOG_LOCK:
                    with open(LOG_FILE) as f:
                        rows = f.read().strip().split("\n")
            except Exception:
                rows = []
            self._send(json.dumps({"rows": rows, "count": max(0, len(rows)-1)}))
        elif u.path == "/log":
            self._send(LOG_PAGE, "text/html")
        elif u.path in ("/", "/index.html"):
            self._send(PAGE, "text/html")
        else:
            self.send_response(404); self.end_headers()
    def log_message(self, *a): pass

def oled_shutdown():
    # power the display off so it doesn't freeze on the last frame when
    # the SBC is shut down / the process is killed
    try:
        d.power(False)
    except Exception:
        pass

import atexit, signal
atexit.register(oled_shutdown)
def _sig(t, f):
    oled_shutdown(); raise SystemExit
signal.signal(signal.SIGTERM, _sig)
signal.signal(signal.SIGINT, _sig)

if __name__ == "__main__":
    threading.Thread(target=scale_reader, daemon=True).start()
    threading.Thread(target=collect, daemon=True).start()
    threading.Thread(target=draw, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", 8080), H)
    try:
        srv.serve_forever()
    finally:
        oled_shutdown()
