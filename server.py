"""本地多智能体协作房间 —— Web GUI 服务。

零第三方依赖，只用标准库。

    python server.py            # 打开 http://127.0.0.1:8787

职责：把 config.json 里的「成员池 + 房间」变成可编辑的界面，
并把 Room 执行过程中的事件通过 SSE 实时推给浏览器。
"""
from __future__ import annotations

import json
import os
import queue
import re
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(ROOT))

from hub.adapters import build          # noqa: E402
from hub.room import Room               # noqa: E402
from hub.store import Store             # noqa: E402

CONFIG_PATH = ROOT / "config.json"
WEB_DIR = ROOT / "web"
DB_PATH = ROOT / "hub.db"
PORT = int(__import__("os").environ.get("HUB_PORT", "8898"))
DEFAULT_MODEL = "deepseek-v4.1-flash"
DEFAULT_EFFORT = "ultracode"
DEFAULT_CMD = ("codebuddy -p -y --model {model} --effort {effort} --port {port}")
EFFORTS = ["minimal", "low", "medium", "high", "xhigh", "max", "ultracode"]

_models_cache: list[str] | None = None

_cfg_lock = threading.Lock()
_room_locks: dict[str, threading.Lock] = {}
_store = Store(DB_PATH)


# ============================ 配置 ============================

def load_config() -> dict:
    with _cfg_lock:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def save_config(cfg: dict) -> None:
    with _cfg_lock:
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                               encoding="utf-8")


def ensure_ports(cfg: dict) -> dict:
    """给没有端口的成员分配一个，避免并行调用时互相抢端口。"""
    used = {m["port"] for m in cfg["members"] if m.get("port")}
    nxt = int(cfg.get("port_base", 3977))
    changed = False
    for m in cfg["members"]:
        if not m.get("port"):
            while nxt in used:
                nxt += 1
            m["port"] = nxt
            used.add(nxt)
            changed = True
    if changed:
        save_config(cfg)
    return cfg


def room_lock(room_id: str) -> threading.Lock:
    if room_id not in _room_locks:
        _room_locks[room_id] = threading.Lock()
    return _room_locks[room_id]


def build_room(room_id: str, on_event=None) -> Room:
    cfg = ensure_ports(load_config())
    room_cfg = next((r for r in cfg["rooms"] if r["id"] == room_id), None)
    if room_cfg is None:
        raise KeyError(f"房间不存在: {room_id}")

    pool = {m["name"]: m for m in cfg["members"]}
    specs = []
    for name in room_cfg.get("members", []):
        m = pool.get(name)
        if not m:
            continue
        spec = dict(m)
        cmd = str(spec.get("cmd", ""))
        cmd = cmd.replace("{port}", str(spec.get("port", 3977)))
        cmd = cmd.replace("{model}", str(spec.get("model") or DEFAULT_MODEL))
        cmd = cmd.replace("{effort}", str(spec.get("effort") or DEFAULT_EFFORT))
        spec["cmd"] = cmd
        specs.append(spec)

    members = [build(s) for s in specs]
    workdir = ROOT / "workspaces" / room_cfg.get("workdir", room_id)
    return Room(_store, room_id, members, workdir,
                default_member=room_cfg.get("default", ""),
                max_hops=int(room_cfg.get("max_hops", 3)),
                max_runs=int(room_cfg.get("max_runs", 1)),
                verbose=False, on_event=on_event)


# ============================ 项目笔记 ============================

def project_path(room_id: str) -> Path:
    cfg = load_config()
    room_cfg = next((r for r in cfg["rooms"] if r["id"] == room_id), None)
    wd = (room_cfg or {}).get("workdir", room_id)
    return ROOT / "projects" / wd / "project.md"


def memory_path(room_id: str, member: str) -> Path:
    cfg = load_config()
    room_cfg = next((r for r in cfg["rooms"] if r["id"] == room_id), None)
    wd = (room_cfg or {}).get("workdir", room_id)
    return ROOT / "projects" / wd / "memory" / f"{member}.md"


def read_project(room_id: str) -> str:
    p = project_path(room_id)
    return p.read_text(encoding="utf-8", errors="ignore") if p.exists() else ""


def write_project(room_id: str, content: str) -> None:
    p = project_path(room_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


# ============================ 预设成员模板 ============================

def _frontmatter(text: str) -> tuple[dict, str]:
    """解析 --- 包起来的元信息，正文就是人格设定。"""
    if not text.startswith("---"):
        return {}, text.strip()
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text.strip()
    meta = {}
    for line in parts[1].strip().splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta, parts[2].strip()


def load_templates() -> list[dict]:
    base = ROOT / "templates"
    if not base.exists():
        return []
    out = []
    for p in sorted(base.glob("*.md")):
        meta, body = _frontmatter(p.read_text(encoding="utf-8", errors="ignore"))
        if not meta.get("name"):
            continue
        out.append({
            "id": p.stem,
            "name": meta.get("name", p.stem),
            "slug": meta.get("slug", p.stem),
            "role": meta.get("role", "成员"),
            "persona": meta.get("persona", meta.get("name", p.stem)),
            "color": meta.get("color", "#4F46E5"),
            "group": meta.get("group", "其他"),
            "description": meta.get("description", ""),
            "system_prompt": body,
        })
    return out


# ============================ 可选模型 ============================

_FALLBACK_MODELS = [
    "deepseek-v4.1-flash", "deepseek-v4.1-flash-sg",
    "gpt-6-astra", "gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna",
    "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5", "gpt-5.4",
    "grok-4.7", "gemini-3.8-flash",
    "glm-5.3", "glm-5.3-flash", "glm-5.2",
    "kimi-k3.1-pro", "kimi-k3.1-pro-highspeed", "kimi-k3",
    "minimax-m3.1-flash", "hy4-preview", "hy3",
    "default-model", "fast-model", "balanced-model", "primary-model",
    "custom-local:Qwen3.8-27B-FP8",
]


def available_models() -> list[str]:
    """优先从 codebuddy --help 解析真实列表，失败则退回内置清单。"""
    global _models_cache
    if _models_cache:
        return _models_cache
    models: list[str] = []
    try:
        import subprocess
        out = subprocess.run("codebuddy --help", shell=True, capture_output=True,
                             text=True, encoding="utf-8", errors="replace",
                             timeout=25).stdout or ""
        m = re.search(r"Currently supported:\s*\(([^)]+)\)", out)
        if m:
            models = [x.strip() for x in m.group(1).split(",") if x.strip()]
    except Exception:
        pass
    _models_cache = models or list(_FALLBACK_MODELS)
    return _models_cache


# ============================ 事件总线 ============================

class EventBus:
    def __init__(self):
        self.subs: dict[str, list[queue.Queue]] = {}
        self.lock = threading.Lock()

    def sub(self, room_id: str) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self.lock:
            self.subs.setdefault(room_id, []).append(q)
        return q

    def unsub(self, room_id: str, q: queue.Queue) -> None:
        with self.lock:
            if q in self.subs.get(room_id, []):
                self.subs[room_id].remove(q)

    def pub(self, room_id: str, event: dict) -> None:
        with self.lock:
            targets = list(self.subs.get(room_id, []))
        for q in targets:
            q.put(event)


BUS = EventBus()


# ============================ 执行 ============================

def run_turn(room_id: str, text: str) -> None:
    """后台线程里跑一轮对话，过程中的事件全部走 BUS。"""
    def emit(kind, data):
        BUS.pub(room_id, {"type": kind, "ts": time.time(), **data})

    lock = room_lock(room_id)
    if not lock.acquire(blocking=False):
        emit("error", {"text": "这个房间还在处理上一条消息，请稍等"})
        return
    try:
        emit("busy", {"on": True})
        room = build_room(room_id, on_event=emit)
        if not room.members:
            emit("error", {"text": "房间里还没有成员，先在右侧加几个"})
            return
        room.say(text)
    except Exception as e:
        traceback.print_exc()
        emit("error", {"text": f"{type(e).__name__}: {e}"})
    finally:
        emit("busy", {"on": False})
        lock.release()


# ============================ HTTP ============================

MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
        ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):        # 静音，避免刷屏
        pass

    def handle_one_request(self):
        # SSE 客户端随时可能关掉页面，这类断连不该打一堆栈
        try:
            super().handle_one_request()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            self.close_connection = True

    # ---------- 工具 ----------
    def _json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _parts(self):
        return [unquote(p) for p in urlparse(self.path).path.strip("/").split("/") if p]

    # ---------- 路由 ----------
    def do_GET(self):
        parts = self._parts()
        try:
            if not parts or parts[0] != "api":
                return self._static(parts)
            if parts[1] == "config":
                return self._json(ensure_ports(load_config()))
            if parts[1] == "templates":
                return self._json({"templates": load_templates()})
            if parts[1] == "models":
                return self._json({"models": available_models(),
                                   "default": DEFAULT_MODEL,
                                   "efforts": EFFORTS,
                                   "default_effort": DEFAULT_EFFORT})
            if parts[1] == "credits":
                cfg = load_config()
                return self._json(cfg.get("credits") or
                                  {"value": None, "unit": "积分", "updated": None})
            if parts[1] == "rooms" and len(parts) >= 4:
                rid = parts[2]
                if parts[3] == "messages":
                    return self._json({"messages": _store.history(rid, 200)})
                if parts[3] == "stream":
                    return self._stream(rid)
                if parts[3] == "project":
                    return self._json({"content": read_project(rid)})
                if parts[3] == "members" and len(parts) >= 6 and parts[5] == "memory":
                    p = memory_path(rid, parts[4])
                    return self._json({"content": p.read_text(encoding="utf-8", errors="ignore")
                                       if p.exists() else ""})
            return self._json({"error": "not found"}, 404)
        except Exception as e:
            traceback.print_exc()
            return self._json({"error": str(e)}, 500)

    def do_POST(self):
        parts = self._parts()
        body = self._body()
        try:
            if parts[:2] == ["api", "members"]:
                return self._save_member(body)
            if parts[:2] == ["api", "credits"]:
                cfg = load_config()
                cur = cfg.get("credits") or {}
                if "value" in body:
                    cur["value"] = body.get("value")
                if "unit" in body:
                    cur["unit"] = body.get("unit") or "积分"
                cur["updated"] = time.strftime("%m-%d %H:%M")
                cfg["credits"] = cur
                save_config(cfg)
                return self._json(cur)
            if parts[:2] == ["api", "rooms"] and len(parts) == 2:
                return self._create_room(body)
            if parts[:2] == ["api", "rooms"] and len(parts) >= 4 and parts[3] == "say":
                rid = parts[2]
                text = (body.get("text") or "").strip()
                if not text:
                    return self._json({"error": "消息不能为空"}, 400)
                threading.Thread(target=run_turn, args=(rid, text), daemon=True).start()
                return self._json({"ok": True})
            if parts[:2] == ["api", "rooms"] and len(parts) >= 4 and parts[3] == "project":
                write_project(parts[2], body.get("content") or "")
                return self._json({"ok": True})
            if parts[:2] == ["api", "rooms"] and len(parts) >= 4 and parts[3] == "archive":
                cfg = load_config()
                room = next((r for r in cfg["rooms"] if r["id"] == parts[2]), None)
                if room is None:
                    return self._json({"error": "房间不存在"}, 404)
                room["archived"] = bool(body.get("archived", True))
                save_config(cfg)
                return self._json({"ok": True, "config": cfg})
            if parts[:2] == ["api", "rooms"] and len(parts) >= 4 and parts[3] == "members":
                if len(parts) >= 6 and parts[5] == "memory":
                    p = memory_path(parts[2], parts[4])
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(body.get("content") or "", encoding="utf-8")
                    return self._json({"ok": True})
                return self._add_room_members(parts[2], body.get("names") or [])
            return self._json({"error": "not found"}, 404)
        except Exception as e:
            traceback.print_exc()
            return self._json({"error": str(e)}, 500)

    def do_PUT(self):
        self.do_POST()

    def do_DELETE(self):
        parts = self._parts()
        try:
            cfg = load_config()

            # 从某个群聊里移除成员（只断开关联，人格和记忆都保留）
            if parts[:2] == ["api", "rooms"] and len(parts) >= 5 and parts[3] == "members":
                rid, name = parts[2], parts[4]
                room = next((r for r in cfg["rooms"] if r["id"] == rid), None)
                if room:
                    room["members"] = [x for x in room["members"] if x != name]
                    if room.get("default") == name:
                        room["default"] = room["members"][0] if room["members"] else ""
                    save_config(cfg)
                return self._json({"ok": True, "config": load_config()})

            # 删除整个群聊
            if parts[:2] == ["api", "rooms"] and len(parts) >= 3:
                cfg["rooms"] = [r for r in cfg["rooms"] if r["id"] != parts[2]]
                save_config(cfg)
                return self._json({"ok": True})

            # 彻底删除成员：人格、所有群聊里的记忆一起清掉
            if parts[:2] == ["api", "members"] and len(parts) >= 3:
                name = parts[2]
                cfg["members"] = [m for m in cfg["members"] if m["name"] != name]
                for r in cfg["rooms"]:
                    r["members"] = [x for x in r.get("members", []) if x != name]
                    if r.get("default") == name:
                        r["default"] = r["members"][0] if r["members"] else ""
                save_config(cfg)
                base = ROOT / "projects"
                if base.exists():
                    for f in base.glob(f"*/memory/{name}.md"):
                        f.unlink()
                return self._json({"ok": True, "config": load_config()})

            return self._json({"error": "not found"}, 404)
        except Exception as e:
            traceback.print_exc()
            return self._json({"error": str(e)}, 500)

    # ---------- 具体动作 ----------
    def _save_member(self, body: dict):
        name = (body.get("name") or "").strip()
        if not name:
            return self._json({"error": "成员名字不能为空"}, 400)
        cfg = ensure_ports(load_config())
        old = next((m for m in cfg["members"] if m["name"] == name), None)
        model = (body.get("model") or "").strip() \
            or (old.get("model") if old else "") or DEFAULT_MODEL
        effort = (body.get("effort") or "").strip() \
            or (old.get("effort") if old else "") or DEFAULT_EFFORT
        member = {
            "name": name,
            "role": body.get("role", "").strip() or "成员",
            "persona": body.get("persona", "").strip() or name,
            "color": body.get("color") or "#4F46E5",
            "type": body.get("type", "cli"),
            "model": model,
            "effort": effort,
            "cmd": body.get("cmd") or DEFAULT_CMD,
            "timeout": int(body.get("timeout") or 600),
            "system_prompt": body.get("system_prompt", ""),
        }
        if old:
            member["port"] = old.get("port")
            cfg["members"] = [member if m["name"] == name else m for m in cfg["members"]]
        else:
            cfg["members"].append(member)
        save_config(cfg)
        ensure_ports(cfg)
        return self._json({"ok": True, "config": load_config()})

    def _add_room_members(self, room_id: str, names: list):
        cfg = ensure_ports(load_config())
        room = next((r for r in cfg["rooms"] if r["id"] == room_id), None)
        if room is None:
            return self._json({"error": "房间不存在"}, 404)
        pool = {m["name"] for m in cfg["members"]}
        for n in names:
            if n in pool and n not in room["members"]:
                room["members"].append(n)
        if not room.get("default") and room["members"]:
            room["default"] = room["members"][0]
        save_config(cfg)
        return self._json({"ok": True, "config": cfg})

    def _create_room(self, body: dict):
        cfg = ensure_ports(load_config())
        name = (body.get("name") or "").strip() or "新群聊"
        rid = f"r{int(time.time() * 1000) % 10_000_000}"
        cfg["rooms"].append({
            "id": rid, "name": name, "workdir": rid,
            "default": "",
            "members": body.get("members") or [],     # 新群聊默认没有成员
        })
        save_config(cfg)
        _store.ensure_room(rid)
        return self._json({"ok": True, "id": rid, "config": cfg})

    def _stream(self, room_id: str):
        q = BUS.sub(room_id)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    ev = q.get(timeout=10)
                    payload = json.dumps(ev, ensure_ascii=False)
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            BUS.unsub(room_id, q)

    def _static(self, parts):
        rel = "/".join(parts) if parts else "index.html"
        path = (WEB_DIR / rel).resolve()
        if not str(path).startswith(str(WEB_DIR.resolve())) or not path.is_file():
            path = WEB_DIR / "index.html"
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(path.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def apply_path_extra(cfg: dict) -> None:
    """子进程不继承 shell 的临时 PATH，npm 全局 bin 得显式补进来。"""
    for p in cfg.get("path_extra") or []:
        d = os.path.expandvars(p)
        if d and d not in os.environ.get("PATH", ""):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")


def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    cfg = ensure_ports(load_config())
    apply_path_extra(cfg)
    WEB_DIR.mkdir(exist_ok=True)
    print(f"多智能体协作房间已启动： http://127.0.0.1:{PORT}")
    print("按 Ctrl+C 停止")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
