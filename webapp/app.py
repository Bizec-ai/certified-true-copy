"""
Certified True Copy tool — local web UI v3.
- Upload ANY certification guidance PDF → auto-analyse requirements
- Upload document to certify
- Draw signature with fingertip (canvas) OR upload PNG
- Drag the chop + certification block into position on previews
- Auto-log every certification job to SQLite (for billing clients)
- Generate locked certified PDF

Run:  uvicorn app:app --host 127.0.0.1 --port 8500
Open: http://127.0.0.1:8500

Set CTC_DEMO=1 for the public demo instance (no login, no job log,
no stored uploads). Nothing personal is hardcoded - the certifier
name, membership line and signature image come from CTC_* env vars.
"""
import base64
import datetime as _dt
import json
import os
import sqlite3
import sys
import tempfile
import uuid
from pathlib import Path

# Run straight from a clone: `uvicorn app:app` inside webapp/ must find the
# `ctc` package that sits one level up.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fitz
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import HTMLResponse, Response, RedirectResponse, JSONResponse
from fastapi import Request
import hashlib
import hmac
import time

# Public demo mode: no login, no job log, no client data. Enabled per-process.
DEMO = os.environ.get("CTC_DEMO", "") == "1"
DEMO_NAME = "CHAN Tai Man (DEMO)"
DEMO_MEMBERSHIP = "DEMO — 非正式認證 / not a valid certification"

DEMO_BANNER = (
    '<div style="background:#fff3cd;border-left:4px solid #f0ad4e;padding:10px 14px;'
    'margin:12px 0;border-radius:8px;font-size:13px;color:#7a5b00">'
    '<b>DEMO 版本</b> — 不登入、不記錄、不儲存任何上載或輸出檔案。'
    '請不要上載真實個人資料。</div>')


from ctc import (
    STAMP_TEXT,
    Certifier,
    best_spot_for_box,
    find_blank_region,
    stamp_box,
)
from ctc import certify_pdf as ctc_certify_pdf
from ctc.guidance import parse_requirements, summarize

# Who signs the certification.  Kept out of the source on purpose: a fork
# starts blank and sets its own identity in the environment.
DEFAULT_CERTIFIER_NAME = os.environ.get("CTC_NAME", "")
DEFAULT_CERTIFIER_MEMBERSHIP = os.environ.get("CTC_MEMBERSHIP", "")
DEFAULT_CERTIFIER_POSITION = os.environ.get("CTC_POSITION", "")
DEFAULT_CERTIFIER_INSTITUTION = os.environ.get("CTC_INSTITUTION", "")
DEFAULT_CERTIFIER_ADDRESS = os.environ.get("CTC_ADDRESS", "")
DEFAULT_CERTIFIER_PHONE = os.environ.get("CTC_PHONE", "")

app = FastAPI(title="Charmey Certified True Copy")

# ---------------------------------------------------------------- access gate
# Single-user login.  No record, document or preview is reachable without a
# cookie minted here, so knowing the URL is worth nothing.
HERE = Path(__file__).parent
PASS_FILE = HERE / ".ctc_pass"
SECRET_FILE = HERE / ".ctc_secret"
COOKIE_NAME = "ctc_session"
COOKIE_MAX_AGE = 30 * 86400                     # 30 days, then log in again
PREVIEW_TTL = 2 * 3600                          # previews self-destruct after 2h
_LOGIN_FAILS = {}                               # ip -> [timestamps]


def _secret() -> bytes:
    try:
        return SECRET_FILE.read_text().strip().encode()
    except Exception:
        raw = os.urandom(32).hex()
        SECRET_FILE.write_text(raw)
        os.chmod(SECRET_FILE, 0o600)
        return raw.encode()


def _pass_hash() -> str:
    try:
        return PASS_FILE.read_text().strip()
    except Exception:
        return ""


def _mint() -> str:
    exp = str(int(time.time()) + COOKIE_MAX_AGE)
    sig = hmac.new(_secret(), exp.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{exp}.{sig}"


def _authed(request: Request) -> bool:
    raw = request.cookies.get(COOKIE_NAME, "")
    if "." not in raw:
        return False
    exp, sig = raw.split(".", 1)
    if not exp.isdigit() or int(exp) < time.time():
        return False
    want = hmac.new(_secret(), exp.encode(), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(sig, want)


def _throttled(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _LOGIN_FAILS.get(ip, []) if now - t < 600]
    _LOGIN_FAILS[ip] = hits
    return len(hits) >= 5


def _purge_previews() -> None:
    """Uploaded previews must not live on the server forever."""
    try:
        cutoff = time.time() - PREVIEW_TTL
        for f in PREVIEW_DIR.iterdir():
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
    except Exception:
        pass


LOGIN_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Charmey CTC - 登入</title>
<style>
:root{--teal:#0ABAB5;--ink:#0f2b33;--line:#e3eaef}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f5f7fa;
     font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:var(--ink)}
.card{background:#fff;padding:34px 30px;border-radius:16px;width:min(360px,92vw);
      box-shadow:0 12px 40px rgba(15,43,51,.10);border:1px solid var(--line)}
h1{font-size:19px;margin:0 0 6px}
p.sub{margin:0 0 22px;color:#6b7c86;font-size:13px}
input{width:100%;padding:13px 14px;border:1px solid var(--line);border-radius:10px;
      font-size:16px;margin-bottom:14px}
input:focus{outline:none;border-color:var(--teal)}
button{width:100%;padding:14px;border:0;border-radius:10px;background:var(--teal);
       color:#fff;font-size:16px;font-weight:600;cursor:pointer}
button:active{opacity:.85}
.err{color:#c0392b;font-size:13px;margin:0 0 12px}

.pvbar{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:10px 0}
.pvbar .lbl{font-size:12px;color:#6b7c86;margin-right:2px}
.chip{padding:7px 12px;border:1px solid var(--border);background:#fff;border-radius:999px;
      font-size:13px;cursor:pointer;color:#0d6b68;font-weight:600;width:auto}
.chip.sel{background:var(--teal);color:#fff;border-color:var(--teal)}
.pv img{cursor:crosshair}
.pv .drag.sel{box-shadow:0 0 0 3px rgba(10,186,181,.35)}
.pv .drag.moving{opacity:.85}
.pv .drag::after{content:'\2725';position:absolute;top:-9px;right:-9px;width:18px;height:18px;
      background:var(--teal);color:#fff;border-radius:50%;font-size:11px;line-height:18px;
      display:flex;align-items:center;justify-content:center}
</style></head><body>
<form class="card" method="post" action="login">
  <h1>Charmey 認證副本</h1>
  <p class="sub">私人工具 - 請輸入密碼</p>
  __ERR__
  <input type="password" name="password" placeholder="密碼" autofocus required>
  <button type="submit">登入</button>
</form></body></html>"""


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if _authed(request):
        return RedirectResponse("./", status_code=303)
    return HTMLResponse(LOGIN_PAGE.replace("__ERR__", ""))


@app.post("/login")
def login_submit(request: Request, password: str = Form("")):
    ip = (request.client.host if request.client else "") or "?"
    if _throttled(ip):
        return HTMLResponse(
            LOGIN_PAGE.replace("__ERR__", '<p class="err">嘗試次數過多，請 10 分鐘後再試。</p>'),
            status_code=429,
        )
    got = hashlib.sha256(password.encode()).hexdigest()
    if not _pass_hash() or not hmac.compare_digest(got, _pass_hash()):
        _LOGIN_FAILS.setdefault(ip, []).append(time.time())
        return HTMLResponse(
            LOGIN_PAGE.replace("__ERR__", '<p class="err">密碼不正確。</p>'),
            status_code=401,
        )
    _LOGIN_FAILS.pop(ip, None)
    resp = RedirectResponse("./", status_code=303)
    resp.set_cookie(
        COOKIE_NAME, _mint(), max_age=COOKIE_MAX_AGE,
        httponly=True, secure=True, samesite="lax", path="/",
    )
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse("login", status_code=303)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


@app.middleware("http")
async def _guard(request: Request, call_next):
    _purge_previews()
    path = request.url.path
    if DEMO:
        if path.startswith("/jobs") or path.startswith("/logout"):
            return HTMLResponse("Not found", status_code=404)
        return await call_next(request)
    if path in ("/login", "/logout") or path.startswith("/login"):
        return await call_next(request)
    if _authed(request):
        return await call_next(request)
    if path == "/" or "text/html" in request.headers.get("accept", ""):
        return RedirectResponse("login", status_code=303)
    return JSONResponse({"detail": "unauthorized"}, status_code=401)

# ------------------------------------------------------------ end access gate


# Optional default signature asset — if present, used as default signature
DEFAULT_SIG = Path(os.environ.get("CTC_SIGNATURE", Path(__file__).parent / "signature.png"))
PREVIEW_DIR = (Path(tempfile.gettempdir()) / ("ctc_preview_demo_" + uuid.uuid4().hex[:8])
               if DEMO else Path(tempfile.gettempdir()) / "ctc_preview")
PREVIEW_DIR.mkdir(exist_ok=True)
DB_PATH = Path(os.environ.get("CTC_DB", Path(__file__).parent / "data" / "ctc_jobs.db"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS ctc_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            certified_at TEXT NOT NULL,
            doc_name TEXT,
            client TEXT,
            pages INTEGER,
            fee_hkd REAL,
            notes TEXT,
            cert_date TEXT,
            signature TEXT
        )"""
    )
    return conn


def log_job(doc_name, client, pages, fee, notes, cert_date, signature):
    if DEMO:
        return
    try:
        conn = db()
        conn.execute(
            "INSERT INTO ctc_jobs (certified_at, doc_name, client, pages, fee_hkd, notes, cert_date, signature) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                doc_name, client, pages, fee, notes, cert_date, signature,
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print("[warn] log_job failed:", e)


HTML = """<!DOCTYPE html>
<html lang="zh-HK">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Charmey — Certified True Copy</title>
<style>
:root{--teal:#0ABAB5;--teal-dark:#089b9b;--bg:#f5f7fa;--border:#e8edf2;--text:#1a2b3a;--muted:#5a6a7a}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,'PingFang HK','Microsoft JhengHei',sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
header{background:linear-gradient(135deg,#0ABAB5,#067d7a);color:#fff;padding:22px 28px}
header h1{font-size:20px;font-weight:700}
header p{font-size:13px;opacity:.9;margin-top:4px}
main{max-width:800px;margin:28px auto;padding:0 18px}
.card{background:#fff;border:1px solid var(--border);border-radius:14px;padding:26px;margin-bottom:20px;box-shadow:0 2px 10px rgba(10,186,181,.06)}
.card h2{font-size:16px;margin-bottom:14px;color:var(--teal-dark)}
label{display:block;font-size:13px;font-weight:600;margin:12px 0 5px;color:var(--muted)}
input[type=text],input[type=number],textarea{width:100%;padding:10px 12px;border:1px solid var(--border);border-radius:8px;font-size:14px;outline:none}
input:focus,textarea:focus{border-color:var(--teal)}
input[type=file]{padding:8px 0}
.row{display:flex;gap:14px}.row>div{flex:1}
.checks{display:flex;gap:20px;margin-top:10px;font-size:13px;flex-wrap:wrap}
.checks label{font-weight:400;margin:0;display:flex;align-items:center;gap:6px}
button{width:100%;margin-top:16px;padding:12px;background:var(--teal);color:#fff;border:none;border-radius:10px;font-size:15px;font-weight:700;cursor:pointer}
button:hover{background:var(--teal-dark)}
button:disabled{opacity:.5;cursor:wait}
button.ghost{background:#fff;color:var(--teal);border:2px solid var(--teal)}
.hint{font-size:12px;color:var(--muted);margin-top:6px}
#sigpad{width:100%;height:180px;border:2px dashed var(--border);border-radius:10px;background:#fff;touch-action:none;cursor:crosshair;margin-top:6px}
#reqbox{background:#f0fdfc;border:1px solid #b2ece9;border-radius:10px;padding:14px;font-size:13px;line-height:1.7;color:#075e5b;margin-top:10px;display:none;white-space:pre-wrap}
#previewArea{margin-top:14px;display:none}
.pv{position:relative;display:inline-block;border:1px solid var(--border);border-radius:8px;overflow:hidden;background:#fff;margin-bottom:14px}
.pv img{display:block;max-width:100%;height:auto}
.pv .drag{position:absolute;cursor:move;touch-action:none;user-select:none;border:2px dashed rgba(10,186,181,.8);background:rgba(10,186,181,.08);font-weight:700;color:#c0392b;display:flex;align-items:center;justify-content:center;text-align:center;border-radius:4px;transform:translate(-50%,-50%)}
.pv .drag.chop{font-size:11px;line-height:1.15;transform:translate(-50%,-50%) rotate(-6deg)}
.pv .drag.block{color:#075e5b;border-color:#0ABAB5;background:rgba(10,186,181,.12);font-size:9px;line-height:1.4}
.pv .tag{position:absolute;top:4px;left:4px;background:rgba(0,0,0,.55);color:#fff;font-size:11px;padding:2px 8px;border-radius:4px}
table{width:100%;border-collapse:collapse;font-size:13px;margin-top:10px}
th,td{padding:8px 10px;border-bottom:1px solid var(--border);text-align:left}
th{background:#f0fdfc;color:var(--teal-dark);font-weight:700}
#status{margin-top:14px;font-size:14px;display:none;font-weight:600}
footer{text-align:center;color:var(--muted);font-size:12px;padding:24px}
</style>
</head>
<body>
<header>
  <h1>📄 Charmey — Certified True Copy 核證副本</h1>
  <p>上傳任何認證指引 → 自動分析要求 → 拖位 + 指尖簽名 → 一鍵出鎖定核證副本（自動記帳）</p>
</header>
<main>
  <div class="card">
    <h2>1️⃣ 認證指引（可選 — 唔上傳就用預設標準）</h2>
    <label>上傳指引 PDF（例如 ASCENT / HKCGI / 銀行要求）</label>
    <input type="file" id="guidance" accept=".pdf">
    <button type="button" class="ghost" id="analyzeBtn" style="margin-top:10px">🔍 分析要求</button>
    <div id="reqbox"></div>
  </div>

  <form class="card" id="form" method="post" action="certify" enctype="multipart/form-data">
    <h2>2️⃣ 要認證嘅文件</h2>
    <label>PDF 文件 *</label>
    <input type="file" name="pdf" id="pdffile" accept=".pdf" required>
    <div class="pvbar">
      <span class="lbl">調整：</span>
      <button type="button" class="chip sel" id="selChop">印章 chop</button>
      <button type="button" class="chip" id="selBlock">認證欄</button>
      <button type="button" class="chip" id="autoBtn">✨ 自動放空白位</button>
      <span class="hint" id="posInfo" style="margin:0 0 0 auto">選上 PDF 即出現預覽</span>
    </div>
    <div id="previewArea">
      <div class="pv" id="pv1">
        <div class="tag">第 1 頁 — 拖紅色 chop，或直接點頁面任何位置</div>
        <img id="img1" src="" alt="page1">
        <div class="drag chop sel" id="chop1">CERTIFIED<br>TRUE COPY</div>
      </div>
      <div class="pv" id="pvL">
        <div class="tag" id="tagL">最後一頁 — 拖動認證 block</div>
        <img id="imgL" src="" alt="last">
        <div class="drag block" id="blockL">Certified True Copy<br>Date: __TODAY__<br>________________<br>__NAME__<br>__MEMBERSHIP__</div>
      </div>
    </div>
    <button type="button" class="ghost" id="previewBtn" style="display:none">重新載入預覽</button>
    <input type="hidden" name="preview_token" id="preview_token" value="">
    <input type="hidden" name="chop_pos" id="chop_pos" value="">
    <input type="hidden" name="block_pos" id="block_pos" value="">
    <div class="checks">
      <label><input type="checkbox" name="strip_blanks" checked> 自動剝走空白頁</label>
      <label><input type="checkbox" name="stamp_every_page" checked> 每頁蓋印 CERTIFIED TRUE COPY（預設開）</label>
    </div>

    <h2 style="margin-top:22px">3️⃣ 簽名（用手指畫 ✍️ 或上傳 PNG）</h2>
    <label>喺下面畫你嘅簽名（可用手指/滑鼠）</label>
    <canvas id="sigpad"></canvas>
    <div class="row" style="margin-top:8px">
      <div><button type="button" class="ghost" id="clearSig" style="margin-top:0">清除重畫</button></div>
      <div><label style="margin-top:0">或上傳簽名圖片</label><input type="file" id="sigfile" name="sig" accept=".png,.jpg,.jpeg"></div>
    </div>
    <input type="hidden" name="sig_data" id="sig_data">

    <div class="row">
      <div><label>姓名</label><input type="text" name="name" value="__NAME__"></div>
      <div><label>Membership</label><input type="text" name="membership" value="__MEMBERSHIP__"></div>
    </div>
    <div class="row">
      <div><label>公司（full 版用）</label><input type="text" name="company" placeholder="例如：Animoca Brands Limited"></div>
      <div><label>職位（full 版用）</label><input type="text" name="capacity" placeholder="例如：Stock Admin Lead"></div>
    </div>
    <div class="row">
      <div><label>專業資格機構</label><input type="text" name="institution" value="__INSTITUTION__"></div>
      <div><label>日期</label><input type="text" name="date" value="__TODAY__"></div>
    </div>
    <label>聯絡地址 / 電話（full 版用，可留空）</label>
    <input type="text" name="address" value="__ADDRESS__">
    <div class="row">
      <div><label>電話</label><input type="text" name="phone" value="__PHONE__"></div>
    </div>

    <h2 style="margin-top:22px">5️⃣ 記帳（用嚟 bill 客）</h2>
    <div class="row">
      <div><label>客戶 Client</label><input type="text" name="client" placeholder="例如：ABC Limited"></div>
      <div><label>收費 HK$</label><input type="number" name="fee" value="500" step="10"></div>
    </div>
    <label>備註 Notes</label>
    <input type="text" name="notes" placeholder="例如：BR certified for bank">

    <input type="hidden" name="reqs" id="reqs" value="">
    <button type="submit" id="btn">生成 Certified True Copy</button>
    <div class="hint">🔒 輸出會鎖定（AES-256）：只可以睇 + 列印，唔可以修改/註釋/複製。每次認證自動記錄喺資料庫。</div>
    <div id="status"></div>
  </form>

  <div class="card">
    <h2>📒 認證記錄（可查帳）</h2>
    <a href="jobs" style="color:var(--teal);font-weight:600;font-size:14px">睇全部記錄 →</a>
  </div>
</main>
<footer>Charmey AI CoSec · data stays on your device 🔒</footer>

<script>
const canvas = document.getElementById('sigpad');
const ctx = canvas.getContext('2d');
const sigData = document.getElementById('sig_data');
let drawing = false;
function resizeCanvas(){ const r = canvas.getBoundingClientRect(); canvas.width = r.width * 2; canvas.height = 180 * 2; ctx.scale(2,2); ctx.strokeStyle = '#1a2b3a'; ctx.lineWidth = 2.2; ctx.lineCap = 'round'; }
resizeCanvas(); window.addEventListener('resize', resizeCanvas);
function pos(e){ const r = canvas.getBoundingClientRect(); const t = e.touches ? e.touches[0] : e; return {x: t.clientX - r.left, y: t.clientY - r.top}; }
canvas.addEventListener('pointerdown', e => { drawing = true; ctx.beginPath(); const p = pos(e); ctx.moveTo(p.x, p.y); });
canvas.addEventListener('pointermove', e => { if(!drawing) return; const p = pos(e); ctx.lineTo(p.x, p.y); ctx.stroke(); });
canvas.addEventListener('pointerup', () => { drawing = false; });
document.getElementById('clearSig').addEventListener('click', () => { ctx.clearRect(0,0,canvas.width,canvas.height); sigData.value=''; });


// ---- overlay placement: drag (pointer capture, iOS-safe), tap, presets ----
var SEL = 'chop';
var AUTO = {chop:null, block:null};
var LAST = {chop:[0.5,0.5], block:[0.08,0.78]};

function _el(w){ return document.getElementById(w==='chop' ? 'chop1' : 'blockL'); }
function _img(w){ return document.getElementById(w==='chop' ? 'img1' : 'imgL'); }

function setPos(w, x, y){
  x = Math.max(0.04, Math.min(0.96, x));
  y = Math.max(0.04, Math.min(0.96, y));
  var el = _el(w); if(!el) return;
  el.style.left = (x*100) + '%';
  el.style.top  = (y*100) + '%';
  var hid = document.getElementById(w==='chop' ? 'chop_pos' : 'block_pos');
  if(hid) hid.value = JSON.stringify([x,y]);
  LAST[w] = [x,y];
  updateInfo();
}

function updateInfo(){
  var info = document.getElementById('posInfo'); if(!info) return;
  info.textContent = 'chop ' + Math.round(LAST.chop[0]*100) + '%/' + Math.round(LAST.chop[1]*100)
                   + '%  ·  認證欄 ' + Math.round(LAST.block[0]*100) + '%/' + Math.round(LAST.block[1]*100) + '%';
}

function selectOverlay(w){
  SEL = w;
  var c = _el('chop'), bl = _el('block');
  if(c) c.classList.toggle('sel', w==='chop');
  if(bl) bl.classList.toggle('sel', w==='block');
  var sc = document.getElementById('selChop'), sb = document.getElementById('selBlock');
  if(sc) sc.classList.toggle('sel', w==='chop');
  if(sb) sb.classList.toggle('sel', w==='block');
}

function bindDrag(w){
  var el = _el(w), img = _img(w);
  if(!el || !img) return;
  var dragging = false, ox = 0, oy = 0, raf = null, pid = null;
  el.addEventListener('pointerdown', function(e){
    e.preventDefault(); e.stopPropagation();
    selectOverlay(w);
    dragging = true; pid = e.pointerId;
    try { el.setPointerCapture(pid); } catch(_) {}
    var r = img.getBoundingClientRect(), er = el.getBoundingClientRect();
    ox = (e.clientX - (er.left + er.width/2)) / r.width;
    oy = (e.clientY - (er.top + er.height/2)) / r.height;
    el.classList.add('moving');
  });
  el.addEventListener('pointermove', function(e){
    if(!dragging) return;
    e.preventDefault();
    var r = img.getBoundingClientRect(), cx = e.clientX, cy = e.clientY;
    if(raf) cancelAnimationFrame(raf);
    raf = requestAnimationFrame(function(){
      setPos(w, (cx - r.left)/r.width - ox, (cy - r.top)/r.height - oy);
    });
  });
  function end(){
    if(!dragging) return;
    dragging = false;
    el.classList.remove('moving');
    try { el.releasePointerCapture(pid); } catch(_) {}
  }
  el.addEventListener('pointerup', end);
  el.addEventListener('pointercancel', end);
  el.addEventListener('lostpointercapture', end);
}

function bindTap(w){
  var img = _img(w); if(!img) return;
  img.addEventListener('click', function(e){
    var r = img.getBoundingClientRect();
    setPos(w, (e.clientX - r.left)/r.width, (e.clientY - r.top)/r.height);
    selectOverlay(w);
  });
}

var PREVIEW_TOKEN = '';
function runPreview(){
  var f = document.getElementById('pdffile');
  if(!f.files || !f.files.length){ return; }
  document.getElementById('previewArea').style.display = 'block';
  var info = document.getElementById('posInfo');
  if(info) info.textContent = '載入預覽…';
  var fd = new FormData(); fd.append('pdf', f.files[0]);
  fetch('preview-setup', {method:'POST', body: fd})
    .then(function(r){ return r.json(); })
    .then(function(d){
      PREVIEW_TOKEN = d.token;
      document.getElementById('preview_token').value = d.token;
      document.getElementById('img1').src = d.page1_url;
      document.getElementById('imgL').src = d.last_url;
      document.getElementById('tagL').textContent = '最後一頁 (第 ' + d.last_page + ' 頁) — 拖認證欄';
      AUTO.chop  = d.auto_chop  || null;
      AUTO.block = d.auto_block || null;
      var c = AUTO.chop  || [0.5, 0.5];
      var bk = AUTO.block || [0.08, 0.78];
      setPos('chop', c[0], c[1]);
      setPos('block', bk[0], bk[1]);
      selectOverlay('chop');
    })
    .catch(function(e){ if(info) info.textContent = '預覽失敗：' + e.message; });
}

(function(){
  var btn = document.getElementById('previewBtn');
  if(btn) btn.addEventListener('click', runPreview);
  var f = document.getElementById('pdffile');
  if(f) f.addEventListener('change', runPreview);
  var sc = document.getElementById('selChop');
  if(sc) sc.addEventListener('click', function(){ selectOverlay('chop'); });
  var sb = document.getElementById('selBlock');
  if(sb) sb.addEventListener('click', function(){ selectOverlay('block'); });
  var ab = document.getElementById('autoBtn');
  if(ab) ab.addEventListener('click', function(){
    var c = AUTO.chop  || [0.5, 0.5];
    var b2 = AUTO.block || [0.08, 0.78];
    setPos('chop', c[0], c[1]);
    setPos('block', b2[0], b2[1]);
  });
  bindDrag('chop'); bindDrag('block');
  bindTap('chop');  bindTap('block');
})();

// Guidance analysis — updates preview block + highlights required fields
document.getElementById('analyzeBtn').addEventListener('click', function(){
  const g = document.getElementById('guidance');
  const box = document.getElementById('reqbox');
  if (!g.files || g.files.length === 0) { box.style.display='block'; box.textContent='請先選擇指引 PDF'; return; }
  const fd = new FormData(); fd.append('guidance', g.files[0]);
  box.style.display='block'; box.textContent='⏳ 分析緊…';
  fetch('analyze', {method:'POST', body: fd})
    .then(r => r.json())
    .then(d => {
      box.textContent = d.summary;
      document.getElementById('reqs').value = JSON.stringify(d.reqs);
      const rq = d.reqs;
      // 1) update the preview block text to show required wordings
      const blk = document.getElementById('blockL');
      const fld = n => (document.querySelector('input[name=' + n + ']') || {}).value || '';
      let html = (rq.statement_wording || 'Certified True Copy') + '<br>Date: ...<br>________________<br>' + fld('name');
      if (rq.requires_institution && fld('institution')) html += '<br>' + fld('institution');
      if (fld('membership')) html += '<br>' + fld('membership');
      if (rq.requires_contact_address || rq.requires_phone) html += '<br>[公司/地址/電話 — 填下面 4️⃣]';
      if (rq.requires_bearer_signed_clause) html += '<br><i>And the bearer of the document has signed in my presence.</i>';
      blk.innerHTML = html;
      // 2) highlight required-but-empty form fields
      const fields = [];
      if (rq.requires_contact_address) fields.push(document.querySelector('input[name=address]'));
      if (rq.requires_phone) fields.push(document.querySelector('input[name=phone]'));
      fields.forEach(f => { if(f){ f.style.borderColor='#e67e22'; f.style.borderWidth='2px'; f.placeholder='必填（指引要求）'; } });
      // 3) show the preview area so she can see the block
      const pa = document.getElementById('previewArea');
      if (pa.style.display !== 'block' && document.getElementById('pdffile').files.length) {
        document.getElementById('previewBtn').click();
      }
    })
    .catch(e => { box.textContent='❌ 分析失敗: '+e.message; });
});

// Submit: canvas signature → blob; then POST
document.getElementById('form').addEventListener('submit', function(ev){
  const btn = document.getElementById('btn');
  btn.disabled = true; btn.textContent = '處理緊…';
  const s = document.getElementById('status'); s.style.display='block'; s.textContent='⏳ 生成緊…';
  const fileInput = document.getElementById('sigfile');
  if (!fileInput.files || fileInput.files.length === 0) {
    const hasInk = canvas.toDataURL().length > 5000;
    if (hasInk) {
      canvas.toBlob(function(blob){
        const fd = new FormData(ev.target);
        fd.set('sig', blob, 'signature.png');
        fetch(ev.target.action, {method:'POST', body: fd})
          .then(r => { if(!r.ok) throw new Error('HTTP '+r.status); return r.blob(); })
          .then(b => { const a=document.createElement('a'); a.href=URL.createObjectURL(b); a.download='certified_true_copy.pdf'; a.click(); btn.disabled=false; btn.textContent='生成 Certified True Copy'; s.textContent='✅ 搞掂！已下載 + 已記帳'; })
          .catch(e => { s.textContent='❌ 出錯: '+e.message; btn.disabled=false; btn.textContent='生成 Certified True Copy'; });
      }, 'image/png');
      ev.preventDefault();
    }
  }
});
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index():
    today = _dt.date.today().strftime("%d/%m/%Y")
    if DEMO:
        html = (HTML
                .replace("__TODAY__", today)
                .replace("__NAME__", DEMO_NAME)
                .replace("__POSITION__", "DEMO")
                .replace("__INSTITUTION__", "DEMO")
                .replace("__MEMBERSHIP__", DEMO_MEMBERSHIP)
                .replace("__ADDRESS__", "")
                .replace("__PHONE__", ""))
        html = html.replace("<body>", "<body>" + DEMO_BANNER, 1)
        i = html.find('<h2>📒 認證記錄（可查帳）</h2>')
        if i > 0:
            j = html.rfind('<div class="card">', 0, i)
            k = html.find('</div>', html.find('</a>', i))
            if j > 0 and k > 0:
                html = html[:j] + html[k+6:]
        return html
    return (HTML
            .replace("__TODAY__", today)
            .replace("__NAME__", DEFAULT_CERTIFIER_NAME)
            .replace("__POSITION__", DEFAULT_CERTIFIER_POSITION)
            .replace("__INSTITUTION__", DEFAULT_CERTIFIER_INSTITUTION)
            .replace("__MEMBERSHIP__", DEFAULT_CERTIFIER_MEMBERSHIP)
            .replace("__ADDRESS__", DEFAULT_CERTIFIER_ADDRESS)
            .replace("__PHONE__", DEFAULT_CERTIFIER_PHONE))


@app.post("/analyze")
async def analyze(guidance: UploadFile = File(...)):
    data = await guidance.read()
    doc = fitz.open(stream=data, filetype="pdf")
    text = "\n".join(p.get_text() for p in doc)
    doc.close()
    reqs = parse_requirements(text, source_name=guidance.filename)
    return {"reqs": reqs.to_dict(), "summary": "\n".join(summarize(reqs))}


@app.post("/preview-setup")
async def preview_setup(pdf: UploadFile = File(...)):
    token = uuid.uuid4().hex[:12]
    data = await pdf.read()
    path = PREVIEW_DIR / f"{token}.pdf"
    path.write_bytes(data)
    doc = fitz.open(path)
    last = len(doc)
    auto_chop = None
    if len(doc) > 0:
        try:
            _bw, _bh = stamp_box(doc[0], "CERTIFIED TRUE COPY")
            auto_chop = best_spot_for_box(doc[0], _bw + 12, _bh + 12, prefer=(0.82, 0.82))
        except Exception:
            auto_chop = find_blank_region(doc[0])
    auto_block = None
    if last > 0:
        try:
            auto_block = best_spot_for_box(doc[last - 1], 312.0, 162.0, prefer=(0.22, 0.80))
        except Exception:
            auto_block = find_blank_region(doc[last - 1], bottom_bias=0.4)
    doc.close()
    return {
        "token": token,
        "page1_url": f"preview/{token}/1",
        "last_url": f"preview/{token}/{last}",
        "last_page": last,
        "auto_chop": auto_chop,
        "auto_block": auto_block,
    }


@app.get("/preview/{token}/{page}")
def preview_png(token: str, page: int):
    path = PREVIEW_DIR / f"{token}.pdf"
    if not path.exists():
        return Response("not found", status_code=404)
    doc = fitz.open(path)
    if page < 1 or page > len(doc):
        doc.close()
        return Response("bad page", status_code=404)
    pix = doc[page - 1].get_pixmap(dpi=80)
    doc.close()
    png = pix.tobytes("png")
    return Response(png, media_type="image/png")


@app.post("/certify")
async def certify(
    pdf: UploadFile = File(...),
    sig: UploadFile | None = File(None),
    sig_data: str = Form(""),
    preview_token: str = Form(""),
    chop_pos: str = Form(""),
    block_pos: str = Form(""),
    name: str = Form(DEFAULT_CERTIFIER_NAME),
    membership: str = Form(DEFAULT_CERTIFIER_MEMBERSHIP),
    company: str = Form(""),
    capacity: str = Form(""),
    institution: str = Form(DEFAULT_CERTIFIER_INSTITUTION),
    address: str = Form(""),
    phone: str = Form(""),
    date: str = Form(""),
    client: str = Form(""),
    fee: float = Form(0),
    notes: str = Form(""),
    reqs: str = Form(""),
    strip_blanks: str = Form("on"),
    stamp_every_page: str = Form(""),
):
    try:
        reqs_dict = json.loads(reqs) if reqs else None
    except Exception:
        reqs_dict = None

    if DEMO:
        name, membership = DEMO_NAME, DEMO_MEMBERSHIP
        client, fee, notes, company, capacity, address, phone = "", 0.0, "", "", "", "", ""
    positions = {}
    try:
        if chop_pos:
            positions["chop"] = json.loads(chop_pos)
        if block_pos:
            positions["block"] = json.loads(block_pos)
    except Exception:
        pass

    with tempfile.TemporaryDirectory() as tmp:
        in_pdf = Path(tmp) / "input.pdf"
        in_pdf.write_bytes(await pdf.read())
        doc_name = pdf.filename or "document.pdf"

        sig_path = None
        if sig and sig.filename:
            sig_path = Path(tmp) / "sig.png"
            sig_path.write_bytes(await sig.read())
        elif sig_data and sig_data.startswith("data:image"):
            sig_path = Path(tmp) / "sig_canvas.png"
            sig_path.write_bytes(base64.b64decode(sig_data.split(",", 1)[1]))
        elif DEFAULT_SIG.exists():
            sig_path = str(DEFAULT_SIG)

        out_pdf = Path(tmp) / "certified.pdf"
        reqs_dict = reqs_dict or {}
        certifier = Certifier(
            name=name,
            membership=membership,
            institution=institution if reqs_dict.get("requires_institution", True) else "",
            capacity=capacity,
            company=company,
            address=address if reqs_dict.get("requires_contact_address", True) else "",
            phone=phone if reqs_dict.get("requires_phone", True) else "",
            heading="Certified True Copy",
            statement=reqs_dict.get("statement_wording", "") or "",
            bearer_signed=bool(reqs_dict.get("requires_bearer_signed_clause", False)),
        )
        r = ctc_certify_pdf(
            str(in_pdf), str(out_pdf),
            certifier=certifier,
            signature=str(sig_path) if sig_path else None,
            date=date or None,
            strip_blanks=(strip_blanks == "on"),
            stamp_pages=(stamp_every_page == "on"),
            stamp_text=STAMP_TEXT,
            block_pos=positions.get("block"),
            stamp_pos=positions.get("chop"),
        )
        pages = r["pages_out"]
        cert_date = date or _dt.date.today().strftime("%d/%m/%Y")
        sig_used = "canvas" if sig_data and not (sig and sig.filename) else ("upload" if sig and sig.filename else ("default" if sig_path else "none"))
        log_job(doc_name, client, pages, fee, notes, cert_date, sig_used)
        data = out_pdf.read_bytes()

    return Response(
        data,
        media_type="application/pdf",
        headers={
            "Content-Disposition": 'attachment; filename="certified_true_copy.pdf"',
        },
    )


JOBS_KEY_FILE = Path(os.environ.get("CTC_JOBS_KEY_FILE", Path(__file__).parent / ".jobs_key"))


def _jobs_key() -> str:
    env = os.environ.get("CTC_JOBS_KEY", "")
    if env:
        return env
    try:
        return JOBS_KEY_FILE.read_text().strip()
    except Exception:
        return ""


@app.get("/jobs", response_class=HTMLResponse)
def jobs():
    if DEMO:
        return HTMLResponse("Not found", status_code=404)
    conn = db()
    rows = conn.execute("SELECT * FROM ctc_jobs ORDER BY id DESC").fetchall()
    total_fee = sum(r["fee_hkd"] or 0 for r in rows)
    conn.close()
    trs = "".join(
        f"<tr><td>{r['certified_at']}</td><td>{r['doc_name']}</td><td>{r['client'] or ''}</td>"
        f"<td>{r['pages']}</td><td>${r['fee_hkd'] or 0:.0f}</td><td>{r['notes'] or ''}</td></tr>"
        for r in rows
    )
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>CTC 認證記錄</title>
<style>body{{font-family:-apple-system,'PingFang HK',sans-serif;background:#f5f7fa;color:#1a2b3a;padding:30px}}
h1{{color:#089b9b;font-size:22px}} table{{width:100%;border-collapse:collapse;background:#fff;margin-top:16px;border-radius:10px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.06)}}
th,td{{padding:10px 12px;border-bottom:1px solid #e8edf2;text-align:left;font-size:13px}} th{{background:#0ABAB5;color:#fff}}
.total{{margin-top:14px;font-size:16px;font-weight:700;color:#089b9b}}
a{{color:#0ABAB5}} .empty{{color:#5a6a7a;margin-top:20px}}
</style></head><body>
<h1>📒 Certified True Copy 認證記錄</h1>
<p><a href="/ctc/">&larr; 返去工具</a></p>
<table><tr><th>時間</th><th>文件</th><th>客戶</th><th>頁數</th><th>收費</th><th>備註</th></tr>{trs or '<tr><td colspan="6" class="empty">未有記錄</td></tr>'}</table>
<div class="total">總收費：HK${total_fee:.0f}</div>
</body></html>"""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("CTC_PORT", "8500")))
