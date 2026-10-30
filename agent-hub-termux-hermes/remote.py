"""远程控制端口：把 agent-hub 房间挂到浏览器上，手机也能发指令。

    python remote.py                    # 默认 0.0.0.0:8899，自动生成令牌
    python remote.py --port 9000        # 换端口
    python remote.py --show-token       # 只打印当前令牌和地址

设计取舍：
  - 零第三方依赖（标准库 http.server），避免在别人电脑上装包
  - 任务走队列 + 轮询，不用长连接：agent 一次要跑几分钟，手机浏览器扛不住长连接
  - 单 worker 串行执行：Room 内部的 _runs 计数不是线程安全的，并发 say 会互相踩
  - 令牌写在 .remote_token，首次访问带 ?t=<token> 换 cookie，之后不用再带
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import secrets
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(ROOT))

from hub.adapters import build          # noqa: E402
from hub.room import Room               # noqa: E402
from hub.store import Store             # noqa: E402

TOKEN_FILE = ROOT / ".remote_token"
LOG_FILE = ROOT / "remote.log"
COOKIE = "hub_token"


# ---------------------------------------------------------------- 令牌
def load_token() -> str:
    if TOKEN_FILE.exists():
        t = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if t:
            return t
    t = secrets.token_urlsafe(18)
    TOKEN_FILE.write_text(t, encoding="utf-8")
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except Exception:
        pass
    return t


def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ---------------------------------------------------------------- 任务队列
class Job:
    __slots__ = ("id", "text", "status", "created", "started", "ended", "error")

    def __init__(self, jid: str, text: str):
        self.id = jid
        self.text = text
        self.status = "pending"      # pending / running / done / error
        self.created = time.time()
        self.started: float | None = None
        self.ended: float | None = None
        self.error = ""

    def dict(self) -> dict:
        now = time.time()
        elapsed = (self.ended or now) - (self.started or self.created)
        return {"id": self.id, "status": self.status, "elapsed": round(elapsed, 1),
                "error": self.error, "text": self.text[:120]}


class Hub:
    """房间 + 单 worker 队列。所有 say 串行执行，避免 _runs 计数被并发踩乱。"""

    def __init__(self, cfg: dict, room: Room):
        self.cfg = cfg
        self.room = room
        self.q: queue.Queue[Job] = queue.Queue()
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        self.current: Job | None = None
        threading.Thread(target=self._worker, daemon=True).start()

    def submit(self, text: str) -> Job:
        job = Job(secrets.token_hex(4), text)
        with self.lock:
            self.jobs[job.id] = job
            if len(self.jobs) > 200:                      # 只留最近 200 条
                for k in sorted(self.jobs, key=lambda k: self.jobs[k].created)[:-200]:
                    self.jobs.pop(k, None)
        self.q.put(job)
        return job

    def _worker(self) -> None:
        while True:
            job = self.q.get()
            self.current = job
            job.status = "running"
            job.started = time.time()
            try:
                self.room.say(job.text)
                job.status = "done"
            except Exception as e:
                job.status = "error"
                job.error = f"{type(e).__name__}: {e}"
                log(f"[job {job.id}] 失败 {job.error}")
            finally:
                job.ended = time.time()
                self.current = None
                self.q.task_done()

    def state(self, after: int) -> dict:
        msgs = [m for m in self.room.store.history(self.room.room_id, 200) if m["id"] > after]
        with self.lock:
            job = self.current.dict() if self.current else None
            pending = self.q.qsize()
        return {"messages": msgs, "job": job, "pending": pending,
                "members": [{"name": m.name, "role": m.role,
                             "persona": m.spec.get("persona", "")}
                            for m in self.room.members.values()]}


# ---------------------------------------------------------------- 日志
def log(msg: str) -> None:
    line = f"[{time.strftime('%m-%d %H:%M:%S')}] {msg}\n"
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


# ---------------------------------------------------------------- 页面
LOGIN_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>需要令牌</title><style>
body{margin:0;height:100vh;display:grid;place-items:center;background:#f5f6f8;
font:16px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;color:#1a1d23}
.box{width:min(88vw,340px);background:#fff;border:1px solid #e3e6ea;border-radius:14px;
padding:24px;box-shadow:0 6px 24px rgba(16,24,40,.06)}
h1{font-size:16px;margin:0 0 4px}p{color:#6b7280;font-size:13px;margin:0 0 16px}
input{width:100%;box-sizing:border-box;padding:11px 12px;border:1px solid #d8dce2;
border-radius:9px;font-size:15px;outline:none}
input:focus{border-color:#2f6bff;box-shadow:0 0 0 3px rgba(47,107,255,.12)}
button{margin-top:10px;width:100%;padding:11px;border:0;border-radius:9px;background:#2f6bff;
color:#fff;font-size:15px;font-weight:600}
</style></head><body><form class="box" method="post" action="/login">
<h1>输入访问令牌</h1><p>令牌在电脑上的 agent-hub/.remote_token</p>
<input name="token" autocomplete="off" autofocus placeholder="令牌">
<button>进入</button></form></body></html>"""

APP_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#ffffff">
<title>Agent Hub</title><style>
*{-webkit-tap-highlight-color:transparent;box-sizing:border-box}
html,body{height:100%;max-width:100%;overflow-x:hidden}
body{margin:0;background:#f5f6f8;color:#1a1d23;display:flex;flex-direction:column;
font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;
overscroll-behavior:none}
header{position:sticky;top:0;z-index:9;background:rgba(255,255,255,.88);
backdrop-filter:blur(12px);border-bottom:1px solid #e8eaee;
padding:calc(env(safe-area-inset-top) + 10px) 14px 8px}
.top{display:flex;align-items:center;gap:8px}
.title{font-weight:650;font-size:15px;letter-spacing:.2px}
.dot{width:7px;height:7px;border-radius:50%;background:#22c55e;box-shadow:0 0 0 3px rgba(34,197,94,.14)}
.dot.busy{background:#f59e0b;box-shadow:0 0 0 3px rgba(245,158,11,.16);animation:pulse 1.4s infinite}
.dot.off{background:#ef4444;box-shadow:0 0 0 3px rgba(239,68,68,.14)}
@keyframes pulse{50%{opacity:.35}}
.chips{display:flex;gap:6px;overflow-x:auto;padding:8px 0 2px;scrollbar-width:none}
.chips::-webkit-scrollbar{display:none}
.chip{flex:0 0 auto;border:1px solid #e0e3e8;background:#fff;border-radius:999px;
padding:4px 11px;font-size:12.5px;color:#4b5563;white-space:nowrap}
.chip b{font-weight:650;color:#1a1d23}
main{flex:1;width:100%;min-width:0;overflow-y:auto;overflow-x:hidden;padding:14px 14px 8px;
display:flex;flex-direction:column;gap:10px}
.msg{max-width:min(86%,640px);min-width:0;width:fit-content;padding:9px 12px;border-radius:13px;
white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;font-size:14.5px;line-height:1.62;
box-shadow:0 1px 2px rgba(16,24,40,.04)}
.msg.me{align-self:flex-end;background:#2f6bff;color:#fff;border-bottom-right-radius:4px}
.msg.bot{align-self:flex-start;background:#fff;border:1px solid #e8eaee;border-bottom-left-radius:4px}
.who{font-size:11.5px;font-weight:650;margin-bottom:2px;letter-spacing:.2px}
.t-researcher{color:#0d9488}.t-builder{color:#2563eb}.t-reviewer{color:#d97706}
.t-writer{color:#7c3aed}.t-you{color:#9ca3af}
.time{font-size:10.5px;color:#b6bcc6;margin-top:4px;text-align:right}
.msg.me .time{color:rgba(255,255,255,.7)}
.busy{align-self:flex-start;display:flex;align-items:center;gap:8px;background:#fff;
border:1px solid #e8eaee;border-radius:13px;padding:9px 13px;font-size:13.5px;color:#6b7280}
.spin{width:13px;height:13px;border:2px solid #dfe3e8;border-top-color:#2f6bff;
border-radius:50%;animation:rot .8s linear infinite}
@keyframes rot{to{transform:rotate(360deg)}}
footer{position:sticky;bottom:0;background:rgba(255,255,255,.94);backdrop-filter:blur(12px);
border-top:1px solid #e8eaee;padding:9px 12px calc(env(safe-area-inset-bottom) + 9px)}
.row{display:flex;gap:8px;align-items:flex-end}
textarea{flex:1;resize:none;max-height:120px;min-height:42px;padding:10px 12px;font:inherit;
border:1px solid #dfe3e8;border-radius:12px;outline:none;background:#fff;line-height:1.45}
textarea:focus{border-color:#2f6bff;box-shadow:0 0 0 3px rgba(47,107,255,.1)}
button.send{flex:0 0 auto;width:46px;height:42px;border:0;border-radius:12px;background:#2f6bff;
color:#fff;font-size:15px;font-weight:600}
button.send:disabled{background:#c3cbd6}
.hint{font-size:11px;color:#aab1bc;margin-top:6px;text-align:center}
</style></head><body>
<header>
  <div class="top"><span class="dot" id="dot"></span><span class="title">Agent Hub</span>
  <span style="margin-left:auto;font-size:11.5px;color:#aab1bc" id="clock"></span></div>
  <div class="chips" id="chips"></div>
</header>
<main id="stream"></main>
<footer>
  <div class="row">
    <textarea id="box" rows="1" placeholder="说点什么，@名字 指定谁来做"></textarea>
    <button class="send" id="send">↑</button>
  </div>
  <div class="hint">不 @ 任何人 → 默认交给阿研</div>
</footer>
<script>
const S = document.getElementById('stream'), BOX = document.getElementById('box'),
      BTN = document.getElementById('send'), DOT = document.getElementById('dot'),
      CHIPS = document.getElementById('chips');
let lastId = 0, job = null, seen = new Set(), tick = null, typing = null;

function esc(s){return s.replace(/[&<>]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
function hhmm(ts){const d=new Date(ts*1000);return String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0')}
function atBottom(){return S.scrollHeight - S.scrollTop - S.clientHeight < 90}
function toBottom(force){if(force||atBottom()) S.scrollTop = S.scrollHeight}

function bubble(m){
  if(seen.has(m.id)) return; seen.add(m.id);
  const div = document.createElement('div');
  const mine = m.kind === 'say';
  div.className = 'msg ' + (mine ? 'me' : 'bot');
  const cls = 't-' + (mine ? 'you' : m.sender);
  const name = mine ? '你' : (m.sender === 'researcher' ? '阿研' : m.sender === 'builder' ? '阿构'
      : m.sender === 'reviewer' ? '阿审' : m.sender === 'writer' ? '阿述' : m.sender);
  div.innerHTML = (mine ? '' : '<div class="who '+cls+'">'+esc(name)+'</div>')
    + esc(m.content) + '<div class="time">'+hhmm(m.created_at)+'</div>';
  S.appendChild(div);
  toBottom(mine);
}

function showTyping(name, sec){
  if(!typing){
    typing = document.createElement('div');
    typing.className = 'busy';
    typing.innerHTML = '<span class="spin"></span><span id="tlabel"></span>';
    S.appendChild(typing);
  }
  document.getElementById('tlabel').textContent = name + ' 正在处理 · ' + sec + 's';
  toBottom();
}
function hideTyping(){ if(typing){ typing.remove(); typing = null; } }

async function pull(){
  try{
    const r = await fetch('/api/state?after=' + lastId);
    if(r.status === 401){ location.href = '/'; return; }
    const d = await r.json();
    d.messages.forEach(m => { lastId = Math.max(lastId, m.id); bubble(m); });
    if(d.members.length && !CHIPS.children.length){
      d.members.forEach(m => {
        const c = document.createElement('div');
        c.className = 'chip';
        c.innerHTML = '@<b>' + m.name + '</b>';
        c.onclick = () => { BOX.value = (BOX.value + ' @' + m.name + ' ').trimStart(); BOX.focus(); };
        CHIPS.appendChild(c);
      });
    }
    job = d.job;
    DOT.className = 'dot' + (job ? ' busy' : '');
    if(job){
      const nm = (d.members.find(m => job.text.includes('@' + m.name)) || {}).persona || '成员';
      showTyping(nm, Math.round(job.elapsed));
    } else hideTyping();
    if(d.pending) DOT.className = 'dot busy';
  }catch(e){ DOT.className = 'dot off'; }
}

async function send(){
  const text = BOX.value.trim();
  if(!text || BTN.disabled) return;
  BTN.disabled = true; BOX.value = ''; BOX.style.height = 'auto';
  try{
    await fetch('/api/say', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({text})});
    await pull();
  }finally{ BTN.disabled = false; BOX.focus(); }
}

BOX.addEventListener('input', () => {
  BOX.style.height = 'auto';
  BOX.style.height = Math.min(BOX.scrollHeight, 120) + 'px';
});
BOX.addEventListener('keydown', e => {
  if(e.key === 'Enter' && !e.shiftKey && !e.isComposing){ e.preventDefault(); send(); }
});
BTN.onclick = send;
setInterval(pull, 2500);
setInterval(() => { document.getElementById('clock').textContent = hhmm(Date.now()/1000); }, 1000);
pull().then(() => toBottom(true));
</script></body></html>"""


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    hub: Hub
    token: str

    def log_message(self, fmt, *args):
        pass                                   # 不刷屏，写自己的日志

    # ---- 工具
    def _authed(self) -> bool:
        q = parse_qs(urlparse(self.path).query)
        if q.get("t", [""])[0] == self.token:
            return True
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE and v == self.token:
                return True
        return False

    def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8",
              extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(),
                   "application/json; charset=utf-8")

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # ---- 路由
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/healthz":
            return self._send(200, b"ok", "text/plain")
        if path == "/login":
            return self._send(200, LOGIN_HTML.encode())
        if not self._authed():
            return self._send(401, LOGIN_HTML.encode())
        if path == "/":
            q = parse_qs(urlparse(self.path).query)
            if q.get("t"):                      # 用 ?t= 首次访问，换 cookie 并清掉地址栏令牌
                return self._send(302, b"", extra={
                    "Location": "/",
                    "Set-Cookie": f"{COOKIE}={self.token}; Path=/; Max-Age=2592000; SameSite=Lax"})
            return self._send(200, APP_HTML.encode())
        if path == "/api/state":
            after = int(parse_qs(urlparse(self.path).query).get("after", ["0"])[0] or 0)
            return self._json(self.hub.state(after))
        return self._send(404, b"not found", "text/plain")

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/login":
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n).decode("utf-8", "replace")
            tok = parse_qs(raw).get("token", [""])[0].strip()
            if tok != self.token:
                log(f"登录失败 ip={self.client_address[0]}")
                return self._send(401, LOGIN_HTML.encode())
            log(f"登录成功 ip={self.client_address[0]}")
            return self._send(302, b"", extra={
                "Location": "/",
                "Set-Cookie": f"{COOKIE}={self.token}; Path=/; Max-Age=2592000; SameSite=Lax"})
        if not self._authed():
            return self._json({"error": "unauthorized"}, 401)
        if path == "/api/say":
            text = (self._body().get("text") or "").strip()
            if not text:
                return self._json({"error": "empty"}, 400)
            job = self.hub.submit(text)
            log(f"新任务 {job.id}: {text[:80]}")
            return self._json({"job": job.dict()})
        return self._json({"error": "not found"}, 404)


# ---------------------------------------------------------------- 启动
def build_hub() -> tuple[Hub, dict]:
    cfg = json.loads((ROOT / "members.json").read_text(encoding="utf-8"))
    extra = cfg.get("path_extra") or []
    dirs = [str((ROOT / p).resolve()) if p.startswith(".") else os.path.expandvars(p)
            for p in extra]
    os.environ["PATH"] = os.pathsep.join(dirs + [os.environ.get("PATH", "")])

    members = [build(s) for s in cfg["members"] if s.get("type") != "mock"]
    workdir = (ROOT / cfg.get("workdir", "./workspaces/main")).resolve()
    store = Store(ROOT / "hub.db")
    room_id = store.ensure_room(cfg.get("room", "main"))
    room = Room(store, room_id, members, workdir,
                default_member=cfg.get("default_member", ""),
                max_hops=int(cfg.get("max_hops", 3)),
                max_runs=int(cfg.get("max_runs", 1)),
                verbose=False)
    return Hub(cfg, room), cfg


def main() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="agent-hub 远程控制端口")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--show-token", action="store_true")
    args = ap.parse_args()

    token = load_token()
    if args.show_token:
        print(f"令牌：{token}")
        print(f"局域网：http://{lan_ip()}:{args.port}/?t={token}")
        return

    hub, cfg = build_hub()
    Handler.hub = hub
    Handler.token = token

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    ip = lan_ip()
    url = f"http://{ip}:{args.port}/?t={token}"
    log(f"启动 {args.host}:{args.port} 成员={','.join(hub.room.members)}")
    print("=" * 58)
    print(f"  远程控制已就绪   端口 {args.port}")
    print(f"  局域网  {url}")
    print(f"  令牌    {token}")
    print(f"  日志    {LOG_FILE}")
    print("=" * 58, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
