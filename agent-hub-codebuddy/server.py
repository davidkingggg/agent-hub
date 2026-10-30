"""本地多智能体协作房间 —— Web GUI 服务。

零第三方依赖，只用标准库。

    python server.py            # 打开 http://127.0.0.1:8898

职责：把 config.json 里的「成员池 + 房间」变成可编辑的界面，
并把 Room 执行过程中的事件通过 SSE 实时推给浏览器。
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import queue
import re
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(ROOT))

from hub.adapters import build          # noqa: E402
from hub.room import Room               # noqa: E402
from hub.store import Store             # noqa: E402
from hub import vault                   # noqa: E402
from hub import project as proj_name    # noqa: E402

CONFIG_PATH = ROOT / "config.json"
WEB_DIR = ROOT / "web"
DB_PATH = ROOT / "hub.db"
VAULT_DIR = ROOT / "projects"           # Obsidian 仓库根，就是 projects/
PORT = int(__import__("os").environ.get("HUB_PORT", "8898"))
DEFAULT_MODEL = "deepseek-v4.1-flash"
DEFAULT_EFFORT = "ultracode"
DEFAULT_CMD = ("codebuddy -p -y --model {model} --effort {effort} --port {port}")
EFFORTS = ["minimal", "low", "medium", "high", "xhigh", "max", "ultracode"]

# ---------- 附件与文件预览 ----------
MAX_UPLOAD = 25 * 1024 * 1024        # 单个文件上限，超过直接拒
MAX_BODY = 64 * 1024 * 1024          # 请求体上限：base64 上传 25MB 约 33MB，留足余量
MAX_RAW = 200 * 1024 * 1024          # /raw 单次吐出的上限，超过让用户本地打开
MAX_TEXT_PREVIEW = 400_000           # 文本预览最多读这么多字符
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".svg", ".avif"}
TEXT_EXT = {
    ".md", ".markdown", ".txt", ".log", ".csv", ".tsv", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".py", ".js", ".mjs", ".ts", ".jsx", ".tsx", ".css",
    ".html", ".htm", ".xml", ".sql", ".sh", ".bat", ".ps1", ".java", ".go", ".rs",
    ".c", ".h", ".cpp", ".rb", ".php", ".lua", ".r",
}
SKIP_DIRS = {"__pycache__", "node_modules", ".git", ".venv", "venv", ".idea"}

# 模型名会被拼进 shell 命令，只允许这些字符
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/:]{0,63}$")

# 成员名：会被拼进 @提及、记忆文件名、glob 模式
MEMBER_RE = re.compile(r"[\w\u4e00-\u9fa5-]{1,32}")

# 允许用系统默认程序直接打开的扩展名。exe/bat/ps1 这类一律只让「定位」，
# 不然上传一个可执行文件再点「打开」就等于执行了它。
OPEN_SAFE_EXT = IMAGE_EXT | {
    ".html", ".htm", ".md", ".txt", ".pdf", ".csv", ".json", ".log",
    ".py", ".js", ".css", ".xml", ".yml", ".yaml", ".toml", ".ini",
}
# 预览时明确指定类型，别指望 Windows 注册表里的 mimetypes
RAW_TYPES = {
    ".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8",
    ".md": "text/plain; charset=utf-8", ".txt": "text/plain; charset=utf-8",
    ".json": "application/json; charset=utf-8", ".csv": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml", ".pdf": "application/pdf",
}

_models_cache: list[str] | None = None

_cfg_lock = threading.Lock()
_room_locks: dict[str, threading.Lock] = {}
_store = Store(DB_PATH)


# ============================ 配置 ============================

def load_config() -> dict:
    with _cfg_lock:
        if not CONFIG_PATH.exists():
            # 首次运行：从示例配置起一份，否则直接崩在 FileNotFoundError 上，
            # 别人 clone 下来根本跑不起来
            example = ROOT / "config.example.json"
            if example.exists():
                CONFIG_PATH.write_text(example.read_text(encoding="utf-8"),
                                       encoding="utf-8")
                print("首次运行，已从 config.example.json 生成 config.json",
                      flush=True)
            else:
                return {"members": [], "rooms": []}
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


def build_room(room_id: str, on_event=None, hint_text: str = "") -> Room:
    cfg = ensure_ports(load_config())
    cfg = ensure_project_names(cfg, hint_text, room_id)
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
    # 有负责人时默认给更多层（一次「派发-验收」要吃 2 层，50 步需要 100 层）
    leader = room_cfg.get("leader", "")
    default_hops = 108 if leader else 6
    # 真正的成本闸门：一次用户请求最多调多少次模型（50 步 × 每步最多 8 人）
    default_calls = 400 if leader else 20
    return Room(_store, room_id, members, workdir,
                default_member=room_cfg.get("default", ""),
                max_hops=int(room_cfg.get("max_hops", default_hops)),
                max_runs=int(room_cfg.get("max_runs", 1)),
                leader=leader,
                leader_max_runs=int(room_cfg.get("leader_max_runs", 50)),
                max_calls=int(room_cfg.get("max_calls", default_calls)),
                verbose=False, on_event=on_event,
                room_name=room_cfg.get("name") or room_id,
                project_name=room_cfg.get("project") or "")


# ============================ 项目笔记 ============================

def project_dir(room_id: str) -> Path:
    """记忆目录按项目名走，不是按房间 id。

    房间不存在时同样不能拿 URL 片段直接拼——`../..` 会跑到 vault 外面去。
    """
    cfg = load_config()
    room_cfg = next((r for r in cfg["rooms"] if r["id"] == room_id), None)
    if room_cfg is None:
        name = safe_filename(room_id) or "_unknown"
    else:
        name = vault.room_dir(room_cfg)
    return VAULT_DIR / name


def project_path(room_id: str) -> Path:
    return project_dir(room_id) / "project.md"


def memory_path(room_id: str, member: str) -> Path:
    """成员记忆文件路径。

    `member` 来自 URL / 请求体，必须当成不可信输入：直接拼进路径的话，
    `../../README` 能读写项目目录之外的任意 .md。这里先剥成纯文件名。
    """
    safe = safe_filename(member)
    if safe.lower().endswith(".md"):
        safe = safe[:-3]
    if not safe or safe in (".", ".."):
        safe = "_"
    return project_dir(room_id) / "memory" / f"{safe}.md"


def read_project(room_id: str) -> str:
    """界面里编辑的只有「项目背景」。自动追加的对话记录不返回，
    否则用户一保存就把整段历史覆盖掉了。"""
    p = project_path(room_id)
    if not p.exists():
        return ""
    return vault.read_section(p, vault.SECTION_BG)


def write_project(room_id: str, content: str) -> None:
    cfg = load_config()
    room = next((r for r in cfg["rooms"] if r["id"] == room_id), None) or {"id": room_id}
    pool = {m["name"]: m for m in cfg["members"]}
    specs = [pool[n] for n in room.get("members", []) if n in pool]
    wd = room.get("workdir") or room_id

    p = project_path(room_id)
    vault.ensure_project(p, vault.project_meta(room), room.get("name") or room_id,
                         vault.project_intro(room, specs, wd))
    vault.write_section(p, vault.SECTION_BG, content)
    vault.touch_updated(p)


# ============================ Obsidian 仓库 ============================

def first_user_message(room_id: str) -> str:
    """这个群最早的一条用户发言，用来给项目起名。"""
    try:
        for m in _store.history(room_id, 500):
            if m.get("kind") == "say":
                return m.get("content") or ""
    except Exception:
        pass
    return ""


def ensure_project_names(cfg: dict, hint_text: str = "", room_id: str = "") -> dict:
    """给还没定名的群聊定一个项目目录名，并把旧目录里的东西搬过去。

    名字一旦写进 config 就不再自动变——目录名变了，记忆就散了。
    """
    def _first(rid: str) -> str:
        msg = first_user_message(rid)
        # 第一条消息这时还没入库（build_room 在 room.say 之前跑），
        # 所以拿「正要发出去的这条」顶上，否则新群聊永远定不了名。
        if not msg and hint_text and rid == room_id:
            return hint_text
        return msg

    if proj_name.assign_names(cfg, VAULT_DIR, _first):
        save_config(cfg)
    return cfg


def project_name_of(room_id: str) -> str:
    """给界面用：没定名就返回空，让用户自己填，别把 r1714593 这种 id 显示出来。"""
    cfg = load_config()
    room = next((r for r in cfg["rooms"] if r["id"] == room_id), None) or {}
    return room.get("project") or ""


def rename_project(room_id: str, new_name: str) -> tuple[bool, str]:
    """改项目目录名。记忆跟着目录走，所以是真的把文件夹改名。"""
    cfg = load_config()
    room = next((r for r in cfg["rooms"] if r["id"] == room_id), None)
    if room is None:
        return False, "房间不存在"

    old = vault.room_dir(room)
    name = proj_name.clean(new_name, limit=40)
    if not name:
        return False, "名字不能为空"
    if name == old:
        return True, old

    taken = {vault.room_dir(r) for r in cfg["rooms"] if r["id"] != room_id}
    name = proj_name.unique(name, taken)

    try:
        vault.adopt_dir(VAULT_DIR / old, VAULT_DIR / name)
    except OSError as e:
        return False, f"目录改名失败：{e}"

    room["project"] = name
    save_config(cfg)
    sync_vault()
    return True, name


def sync_vault() -> None:
    """补齐所有房间的笔记并重建索引。启动时、配置变更后调用。"""
    try:
        vault.sync_vault(VAULT_DIR, ensure_project_names(load_config()))
    except Exception:
        traceback.print_exc()


_last_index_at = 0.0
_index_lock = threading.Lock()


def rebuild_index(force: bool = False) -> None:
    """重建 `_index.md`。

    每轮对话结束都会调，但一轮里可能连续写好几条记忆。
    5 秒内重复调用直接跳过，最后一次会把前面的变化一起补上。
    """
    global _last_index_at
    with _index_lock:
        now = time.time()
        if not force and now - _last_index_at < 5:
            return
        _last_index_at = now
    try:
        cfg = load_config()
        vault.rebuild_index(VAULT_DIR, cfg.get("rooms") or [],
                            cfg.get("members") or [])
    except Exception:
        traceback.print_exc()


# ============================ 工作目录 / 附件 ============================
# 附件直接落在 workspaces/<房间>/uploads/ 下，agent 用自己的工具就能读，
# 不需要把文件内容塞进 prompt。

def workdir_of(room_id: str) -> Path:
    """房间的工作目录。

    room_id 来自 URL，可能是 `../../..` 这种。房间不存在时绝不能拿它直接拼路径，
    否则 /files 能列出整块磁盘、/raw 能读任意文件。
    """
    cfg = load_config()
    room = next((r for r in cfg["rooms"] if r["id"] == room_id), None)
    wd = (room or {}).get("workdir") or safe_filename(room_id) or "_unknown"
    return ROOT / "workspaces" / wd


def safe_join(root: Path, rel: str) -> Path:
    """把相对路径钉死在 root 里。`..`、绝对路径、指向外部的符号链接都要挡住。"""
    rel = (rel or "").replace("\\", "/").lstrip("/")
    p = (root / rel).resolve()
    root_r = root.resolve()
    if p != root_r and root_r not in p.parents:
        raise PermissionError(f"越界：{rel}")
    return p


def safe_filename(name: str) -> str:
    """只留文件名，去掉路径分隔符和 Windows 非法字符，中文原样保留。"""
    name = (name or "").replace("\\", "/").split("/")[-1]
    name = re.sub(r'[<>:"|?*\r\n\t\x00-\x1f]', "_", name).strip(" .")
    if not name:
        return "file"
    stem, ext = os.path.splitext(name)
    if len(name) > 96:
        name = stem[:80] + ext[:16]
    return name


def file_kind(p: Path) -> str:
    ext = p.suffix.lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in TEXT_EXT:
        return "text"
    return "binary"


def memory_stats(room_id: str) -> dict:
    """每个成员在这个项目里积累了多少条记忆。右栏卡片上显示。"""
    d = project_dir(room_id) / "memory"
    out: dict[str, int] = {}
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.md")):
        body = vault.read_section(p, vault.SECTION_MEMORY)
        out[p.stem] = sum(1 for ln in body.splitlines() if ln.strip().startswith("- "))
    return out


def list_workfiles(room_id: str) -> list[dict]:
    """工作目录里的全部文件，最近改的排前面——用户刚传的、成员刚写的都在头部。

    用 os.walk 而不是 rglob：可以剪枝，遇到深目录和 node_modules 直接不进去。
    """
    root = workdir_of(room_id)
    if not root.exists():
        return []
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        if len(rel_dir.parts) >= 6:          # 太深的目录不往下走
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames
                       if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if fn.startswith("."):
                continue
            p = Path(dirpath) / fn
            try:
                st = p.stat()
            except OSError:
                continue
            out.append({"path": p.relative_to(root).as_posix(), "name": fn,
                        "size": st.st_size, "mtime": st.st_mtime,
                        "kind": file_kind(p), "abs": str(p)})
    out.sort(key=lambda f: f["mtime"], reverse=True)
    return out[:500]


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

def run_turn(room_id: str, text: str, attachments: list | None = None) -> None:
    """后台线程里跑一轮对话，过程中的事件全部走 BUS。"""
    def emit(kind, data):
        BUS.pub(room_id, {"type": kind, "ts": time.time(), **data})

    lock = room_lock(room_id)
    if not lock.acquire(blocking=False):
        emit("error", {"text": "这个房间还在处理上一条消息，请稍等"})
        return
    try:
        emit("busy", {"on": True})
        room = build_room(room_id, on_event=emit, hint_text=text)
        if not room.members:
            emit("error", {"text": "房间里还没有成员，先在右侧加几个"})
            return
        room.say(text, attachments=attachments or [])
    except Exception as e:
        traceback.print_exc()
        emit("error", {"text": f"{type(e).__name__}: {e}"})
    finally:
        emit("busy", {"on": False})
        lock.release()
        rebuild_index()      # 这一轮可能写了不少记忆，把索引页刷一下


# ============================ 房间消息队列 ============================
# 同一房间串行执行，不同房间互不影响。
# 为什么不直接并发：Room 内部的 _runs 计数不是线程安全的，
# 两个 say 同时跑会互相踩（同一个成员可能被派两次活）。
# 排队而不是拒绝，是因为「中途想补一句」是正常需求，不该丢消息。

_queues: dict[str, queue.Queue] = {}
_workers: dict[str, threading.Thread] = {}
_running: dict[str, bool] = {}
_queue_lock = threading.Lock()


def enqueue_turn(room_id: str, text: str, attachments: list | None = None) -> int:
    """把消息放进房间队列，返回前面还有几条（0 = 立刻开始跑）。

    注意要把「正在跑的那一条」算进去：它已经出队了，但用户眼里它还在前面。
    """
    with _queue_lock:
        q = _queues.setdefault(room_id, queue.Queue())
        ahead = q.qsize() + (1 if _running.get(room_id) else 0)
        q.put({"text": text, "attachments": attachments or []})
        t = _workers.get(room_id)
        if t is None or not t.is_alive():
            t = threading.Thread(target=_worker_loop, args=(room_id,), daemon=True)
            _workers[room_id] = t
            t.start()
        return ahead


def queue_size(room_id: str) -> int:
    q = _queues.get(room_id)
    return q.qsize() + (1 if _running.get(room_id) else 0) if q else 0


def drop_room_queue(room_id: str) -> None:
    """房间删掉时把队列一起收掉，别让它一直挂在内存里。

    正在跑的那一轮不动——它跑完会自己退出，硬杀线程只会留下半截状态。
    """
    with _queue_lock:
        q = _queues.pop(room_id, None)
        _running.pop(room_id, None)
        t = _workers.get(room_id)
        if t and not t.is_alive():
            _workers.pop(room_id, None)
    if q is not None:
        while True:                       # 丢掉还没开始的任务
            try:
                q.get_nowait()
            except queue.Empty:
                break


def _worker_loop(room_id: str) -> None:
    q = _queues.get(room_id)
    if q is None:                     # 房间刚被删，队列已经收掉了
        return
    while True:
        try:
            job = q.get(timeout=0.6)
        except queue.Empty:
            with _queue_lock:
                # 再确认一次：入队和退出之间有竞态窗口
                if q.empty():
                    # 队列可能已经被 drop_room_queue 换掉或删掉了，
                    # 只清理属于自己这一份的登记，别误删新建的 worker
                    if _queues.get(room_id) is q:
                        _queues.pop(room_id, None)
                        _workers.pop(room_id, None)
                        _running.pop(room_id, None)
                    return
            continue

        _running[room_id] = True
        try:
            if isinstance(job, dict):
                run_turn(room_id, job.get("text", ""), job.get("attachments") or [])
            else:                       # 兼容队列里残留的纯字符串
                run_turn(room_id, str(job))
        except Exception as e:
            traceback.print_exc()
            BUS.pub(room_id, {"type": "error",
                              "ts": time.time(),
                              "text": f"{type(e).__name__}: {e}"})
        finally:
            _running[room_id] = False


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
        if n > MAX_BODY:
            # 不读、不缓冲，直接断开：否则一个超大 Content-Length 就能把内存吃光
            print(f"[warn] 请求体 {n} 字节超过上限 {MAX_BODY}，已拒绝", flush=True)
            self.close_connection = True
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            # 静默返回空字典的话，调用方只会看到「没收到数据」，很难查。
            traceback.print_exc()
            return {}

    def _parts(self):
        return [unquote(p) for p in urlparse(self.path).path.strip("/").split("/") if p]

    # ---------- 路由 ----------
    def do_GET(self):
        parts = self._parts()
        try:
            if not parts or parts[0] != "api":
                if parts and parts[0] == "healthz":
                    return self._json({"ok": True})
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
            if parts[1] == "vault":
                return self._json({
                    "path": str(VAULT_DIR),
                    "index": str(VAULT_DIR / vault.INDEX_NAME),
                })
            if parts[1] == "rooms" and len(parts) >= 4:
                rid = parts[2]
                if parts[3] == "messages":
                    return self._json({"messages": _store.history(rid, 200)})
                if parts[3] == "stream":
                    return self._stream(rid)
                if parts[3] == "project":
                    return self._json({"content": read_project(rid),
                                       "project": project_name_of(rid)})
                if parts[3] == "files":
                    return self._json({"files": list_workfiles(rid)})
                if parts[3] == "memory-stats":
                    return self._json(memory_stats(rid))
                if parts[3] == "file":
                    return self._read_file(rid)
                if parts[3] == "raw":
                    return self._raw_file(rid)
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
            if parts[:2] == ["api", "vault"] and len(parts) >= 3 and parts[2] == "sync":
                sync_vault()
                return self._json({"ok": True, "path": str(VAULT_DIR)})
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
                atts = body.get("attachments") or []
                if not isinstance(atts, list):
                    atts = []
                if not text and not atts:
                    return self._json({"error": "消息不能为空"}, 400)
                ahead = enqueue_turn(rid, text, atts)
                return self._json({"ok": True, "queued": ahead,
                                   "pending": queue_size(rid)})
            if parts[:2] == ["api", "rooms"] and len(parts) >= 4 and parts[3] == "upload":
                return self._upload(parts[2], body)
            if parts[:2] == ["api", "rooms"] and len(parts) >= 4 and parts[3] == "reveal":
                return self._open_local(parts[2], body)
            if parts[:2] == ["api", "rooms"] and len(parts) >= 4 and parts[3] == "project":
                write_project(parts[2], body.get("content") or "")
                return self._json({"ok": True})
            if parts[:2] == ["api", "rooms"] and len(parts) >= 4 and parts[3] == "project-name":
                ok, msg = rename_project(parts[2], body.get("name") or "")
                if not ok:
                    return self._json({"error": msg}, 400)
                return self._json({"ok": True, "project": msg,
                                   "config": load_config()})
            if parts[:2] == ["api", "rooms"] and len(parts) >= 4 and parts[3] == "archive":
                cfg = load_config()
                room = next((r for r in cfg["rooms"] if r["id"] == parts[2]), None)
                if room is None:
                    return self._json({"error": "房间不存在"}, 404)
                room["archived"] = bool(body.get("archived", True))
                save_config(cfg)
                sync_vault()
                return self._json({"ok": True, "config": cfg})
            if parts[:2] == ["api", "rooms"] and len(parts) >= 4 and parts[3] == "leader":
                cfg = load_config()
                room = next((r for r in cfg["rooms"] if r["id"] == parts[2]), None)
                if room is None:
                    return self._json({"error": "房间不存在"}, 404)
                name = (body.get("leader") or "").strip()
                if name and name not in room.get("members", []):
                    return self._json({"error": "该成员不在本群"}, 400)
                room["leader"] = name
                save_config(cfg)
                sync_vault()
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
        body = self._body()
        try:
            cfg = load_config()

            # 删除选中的消息（DB + Obsidian 里对应的对话记录）
            if (parts[:2] == ["api", "rooms"] and len(parts) >= 4
                    and parts[3] == "messages"):
                return self._delete_messages(parts[2], body.get("ids") or [])

            # 从某个群聊里移除成员（只断开关联，人格和记忆都保留）
            if parts[:2] == ["api", "rooms"] and len(parts) >= 5 and parts[3] == "members":
                rid, name = parts[2], parts[4]
                room = next((r for r in cfg["rooms"] if r["id"] == rid), None)
                if room:
                    room["members"] = [x for x in room["members"] if x != name]
                    if room.get("default") == name:
                        room["default"] = room["members"][0] if room["members"] else ""
                    save_config(cfg)
                    sync_vault()
                return self._json({"ok": True, "config": load_config()})

            # 删除整个群聊
            if parts[:2] == ["api", "rooms"] and len(parts) >= 3:
                cfg["rooms"] = [r for r in cfg["rooms"] if r["id"] != parts[2]]
                save_config(cfg)
                drop_room_queue(parts[2])
                sync_vault()
                return self._json({"ok": True})

            # 彻底删除成员：人格、所有群聊里的记忆一起清掉
            if parts[:2] == ["api", "members"] and len(parts) >= 3:
                name = parts[2]
                # name 会被拼进 glob 模式（*/memory/<name>.md），不校验就能删到目录外
                if not MEMBER_RE.fullmatch(name):
                    return self._json({"error": "成员名不合法"}, 400)
                cfg["members"] = [m for m in cfg["members"] if m["name"] != name]
                for r in cfg["rooms"]:
                    r["members"] = [x for x in r.get("members", []) if x != name]
                    if r.get("default") == name:
                        r["default"] = r["members"][0] if r["members"] else ""
                save_config(cfg)
                if VAULT_DIR.exists():
                    for f in VAULT_DIR.glob(f"*/memory/{name}.md"):
                        f.unlink()
                sync_vault()
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
        # 名字会被拼进 @提及、记忆文件名、glob 模式，限制成安全字符
        if not MEMBER_RE.fullmatch(name):
            return self._json(
                {"error": "名字只能用中英文、数字、下划线或连字符，最长 32 个字符"}, 400)
        cfg = ensure_ports(load_config())
        old = next((m for m in cfg["members"] if m["name"] == name), None)
        model = (body.get("model") or "").strip() \
            or (old.get("model") if old else "") or DEFAULT_MODEL
        effort = (body.get("effort") or "").strip() \
            or (old.get("effort") if old else "") or DEFAULT_EFFORT

        # model 会被拼进 shell 命令（adapters._with_model），必须白名单
        if not MODEL_RE.fullmatch(model):
            return self._json({"error": f"模型名不合法：{model}"}, 400)

        fb = body["fallback_models"] if "fallback_models" in body \
            else (old.get("fallback_models") if old else [])
        if not isinstance(fb, list):
            fb = []
        fb = [str(x).strip() for x in fb if str(x).strip()]
        bad = [x for x in fb if not MODEL_RE.fullmatch(x)]
        if bad:
            return self._json({"error": f"备用模型名不合法：{'、'.join(bad)}"}, 400)

        try:
            timeout = int(body.get("timeout") or 600)
        except (TypeError, ValueError):
            timeout = 600
        timeout = max(10, min(timeout, 3600))

        member = {
            "name": name,
            "role": body.get("role", "").strip() or "成员",
            "persona": body.get("persona", "").strip() or name,
            "color": body.get("color") or "#4F46E5",
            "type": body.get("type", "cli"),
            "model": model,
            "effort": effort,
            "fallback_models": fb,
            # cmd 不从 API 接收：它最终会被 shell=True 执行，等于把「执行任意命令」
            # 暴露给任何能发 POST 的页面。要自定义命令行，直接改 config.json。
            "cmd": (old.get("cmd") if old else "") or DEFAULT_CMD,
            "timeout": timeout,
            "system_prompt": body.get("system_prompt", ""),
        }
        if old:
            member["port"] = old.get("port")
            cfg["members"] = [member if m["name"] == name else m for m in cfg["members"]]
        else:
            cfg["members"].append(member)
        save_config(cfg)
        ensure_ports(cfg)
        sync_vault()          # 人格/角色变了，笔记的 frontmatter 和标题要跟着改
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
        sync_vault()          # 新加入的成员要有自己的记忆笔记
        return self._json({"ok": True, "config": cfg})

    # ---------- 附件与文件 ----------
    def _upload(self, room_id: str, body: dict):
        """收 base64 附件，落到 workspaces/<房间>/uploads/。

        用 JSON + base64 而不是 multipart：标准库解析 multipart 又长又容易出错，
        而这里最大的文件也就几十 MB，base64 膨胀 33% 完全可以接受。
        """
        items = body.get("files")
        if items is None and body.get("data"):
            items = [body]
        if not isinstance(items, list) or not items:
            return self._json({"error": "没有收到文件"}, 400)

        root = workdir_of(room_id)
        dest_dir = root / "uploads"
        dest_dir.mkdir(parents=True, exist_ok=True)

        saved, failed = [], []
        for it in items[:20]:
            if not isinstance(it, dict):
                continue
            name = safe_filename(it.get("name") or "file")
            raw_b64 = str(it.get("data") or "")
            if "," in raw_b64:                 # 去掉 data:image/png;base64, 前缀
                raw_b64 = raw_b64.split(",", 1)[1]
            try:
                raw = base64.b64decode(raw_b64)
            except Exception:
                failed.append({"name": name, "why": "base64 解析失败"})
                continue
            if not raw:
                failed.append({"name": name, "why": "空文件"})
                continue
            if len(raw) > MAX_UPLOAD:
                failed.append({"name": name,
                               "why": f"超过 {MAX_UPLOAD // 1024 // 1024}MB 上限"})
                continue
            dest = self._unique(dest_dir, name)
            try:
                dest.write_bytes(raw)
            except OSError as e:
                failed.append({"name": name, "why": f"写入失败：{e}"})
                continue
            saved.append({
                "path": f"uploads/{dest.name}",
                "name": name,
                "size": len(raw),
                "type": it.get("type") or mimetypes.guess_type(name)[0] or "",
                "mtime": time.time(),
                "abs": str(dest),
            })

        if not saved:
            why = failed[0]["why"] if failed else "上传失败"
            return self._json({"error": why, "failed": failed}, 400)
        return self._json({"ok": True, "files": saved, "failed": failed})

    @staticmethod
    def _unique(d: Path, name: str) -> Path:
        """同名不覆盖：加时间戳前缀，还撞就加序号。"""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        stem, ext = os.path.splitext(name)
        dest = d / f"{stamp}-{name}"
        i = 2
        while dest.exists():
            dest = d / f"{stamp}-{stem}({i}){ext}"
            i += 1
        return dest

    def _query_path(self) -> str:
        qs = parse_qs(urlparse(self.path).query)
        return (qs.get("path") or [""])[0]

    def _read_file(self, room_id: str):
        """给预览弹窗用：文本直接返回内容，图片给一个 raw 地址。"""
        rel = self._query_path()
        root = workdir_of(room_id)
        try:
            p = safe_join(root, rel)
        except PermissionError as e:
            return self._json({"error": str(e)}, 403)
        if not p.is_file():
            return self._json({"error": "文件不存在"}, 404)

        st = p.stat()
        kind = file_kind(p)
        info = {
            "path": p.relative_to(root.resolve()).as_posix(),
            "name": p.name,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "kind": kind,
            "abs": str(p),
            "raw": f"/api/rooms/{room_id}/raw?path={quote(rel)}",
        }
        if kind == "text":
            if st.st_size > MAX_TEXT_PREVIEW:
                info["truncated"] = True
                with p.open("r", encoding="utf-8", errors="ignore") as f:
                    info["content"] = f.read(MAX_TEXT_PREVIEW)
            else:
                info["content"] = p.read_text(encoding="utf-8", errors="ignore")
        return self._json(info)

    def _raw_file(self, room_id: str):
        """原样吐字节：图片、PDF、HTML 预览和下载都走这里。"""
        qs = parse_qs(urlparse(self.path).query)
        rel = (qs.get("path") or [""])[0]
        download = (qs.get("download") or [""])[0] in ("1", "true", "yes")
        root = workdir_of(room_id)
        try:
            p = safe_join(root, rel)
        except PermissionError:
            return self._json({"error": "越界"}, 403)
        if not p.is_file():
            return self._json({"error": "文件不存在"}, 404)

        try:
            size = p.stat().st_size
        except OSError:
            return self._json({"error": "读不到这个文件"}, 500)
        if size > MAX_RAW:
            return self._json({
                "error": f"文件 {size // 1024 // 1024}MB，超过预览上限。"
                         "用「定位」在本地打开更快。"}, 413)

        ext = p.suffix.lower()
        ctype = RAW_TYPES.get(ext) or mimetypes.guess_type(p.name)[0] \
            or "application/octet-stream"
        data = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition",
                         f"{'attachment' if download else 'inline'}; "
                         f"filename*=UTF-8''{quote(p.name)}")
        if ext in (".html", ".htm", ".svg"):
            # 成员写的 HTML 可能带脚本，别让它拿到本应用的源
            self.send_header("Content-Security-Policy",
                             "sandbox allow-scripts allow-popups")
        self.end_headers()
        self.wfile.write(data)

    # ---------- 删除消息 ----------
    def _delete_messages(self, room_id: str, ids) -> None:
        """删 DB 里的消息，并同步删掉 Obsidian 里对应的对话记录。

        记忆（memory/*.md）不动——那是成员自己攒的经验摘要，不是对话副本。
        """
        try:
            ids = sorted({int(i) for i in ids})
        except (TypeError, ValueError):
            return self._json({"error": "ids 必须是整数数组"}, 400)
        if not ids:
            return self._json({"error": "没有选中消息"}, 400)

        rows = _store.delete_messages(room_id, ids)
        if not rows:
            return self._json({"error": "这些消息已经不在了"}, 404)

        # 不做 undo，但留一份纯文本备份，误删了还能翻出来
        try:
            trash = project_dir(room_id) / ".trash"
            trash.mkdir(parents=True, exist_ok=True)
            f = trash / f"deleted-{time.strftime('%Y%m%d')}.md"
            with f.open("a", encoding="utf-8") as fh:
                fh.write(f"\n## [{time.strftime('%m-%d %H:%M')}] 删除 {len(rows)} 条\n\n")
                for r in rows:
                    when = time.strftime("%m-%d %H:%M", time.localtime(r["created_at"]))
                    fh.write(f"### {r['sender']} · {when}\n\n{r['content']}\n\n")
        except OSError:
            pass

        # DB 里用户消息的 sender 是「你」，project.md 里写的是「用户」，
        # 老记录按时间+发送者匹配时得先归一化，否则永远对不上。
        keys = [{"id": r["id"],
                 "stamp": time.strftime("%m-%d %H:%M", time.localtime(r["created_at"])),
                 "sender": "用户" if r["sender"] in ("你", "用户") else r["sender"]}
                for r in rows]
        synced, skipped = 0, 0
        try:
            p = project_path(room_id)
            synced, skipped = vault.remove_log_entries(p, keys)
            if synced:
                vault.touch_updated(p)
        except Exception:
            traceback.print_exc()

        return self._json({"ok": True, "deleted": len(rows),
                           "obsidian": synced, "obsidian_skipped": skipped})

    def _open_local(self, room_id: str, body: dict):
        """用系统默认程序打开文件，或者在资源管理器里定位它。

        这是本地服务才有的便利：浏览器下载一遍再打开纯属多余。
        路径照样钉死在工作目录里，传 `..` 出去一律 403。
        """
        rel = body.get("path") or ""
        mode = (body.get("mode") or "reveal").lower()
        root = workdir_of(room_id)
        try:
            p = safe_join(root, rel)
        except PermissionError:
            return self._json({"error": "越界"}, 403)
        if not p.is_file():
            return self._json({"error": "文件不存在"}, 404)

        # 只让「打开」作用于文档类文件。exe/bat/ps1 这些一旦被打开就是执行，
        # 而工作目录里的东西可能是成员写的——不能给一条「点一下就运行」的路径。
        if mode == "open" and p.suffix.lower() not in OPEN_SAFE_EXT:
            return self._json({
                "error": f"出于安全考虑，不让直接打开 {p.suffix or '无扩展名'} 文件，"
                         "可以用「定位」在资源管理器里自己打开。"}, 400)

        try:
            if mode == "open":
                if sys.platform == "win32":
                    os.startfile(str(p))            # type: ignore[attr-defined]
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", str(p)])
                else:
                    subprocess.Popen(["xdg-open", str(p)])
            else:
                if sys.platform == "win32":
                    subprocess.Popen(["explorer", "/select,", str(p)])
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", "-R", str(p)])
                else:
                    subprocess.Popen(["xdg-open", str(p.parent)])
        except Exception as e:
            return self._json({"error": f"打不开：{type(e).__name__}: {e}"}, 500)
        return self._json({"ok": True, "path": str(p)})

    def _create_room(self, body: dict):
        cfg = ensure_ports(load_config())
        kind = (body.get("kind") or "group").strip()
        rid = f"r{int(time.time() * 1000) % 10_000_000}"

        if kind == "direct":
            member = (body.get("member") or "").strip()
            pool = {m["name"]: m for m in cfg["members"]}
            if member not in pool:
                return self._json({"error": "成员不存在"}, 400)
            # 同一个成员只保留一个单聊，重复点就跳过去
            for r in cfg["rooms"]:
                if r.get("kind") == "direct" and r.get("members") == [member]:
                    return self._json({"ok": True, "id": r["id"], "config": cfg,
                                       "existed": True})
            spec = pool[member]
            name = spec.get("persona") or spec.get("role") or member
            cfg["rooms"].append({
                "id": rid, "name": name, "kind": "direct", "workdir": rid,
                "default": member, "members": [member],
            })
        else:
            name = (body.get("name") or "").strip() or "新群聊"
            cfg["rooms"].append({
                "id": rid, "name": name, "kind": "group", "workdir": rid,
                "default": "",
                "members": body.get("members") or [],     # 新群聊默认没有成员
            })

        save_config(cfg)
        _store.ensure_room(rid)
        sync_vault()
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
        root = WEB_DIR.resolve()
        path = (root / rel).resolve()
        # 不能用 startswith 做前缀判断：`web-evil/x` 也以 `web` 开头。
        # 用 parents 判断真实的目录归属。
        if path != root and root not in path.parents:
            path = root / "index.html"
        if not path.is_file():
            path = root / "index.html"
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(path.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        # 本地工具，改完代码刷新就该生效，别让浏览器拿缓存的旧 JS
        self.send_header("Cache-Control", "no-store")
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
    sync_vault()             # 补齐记忆笔记 + 重建 Obsidian 索引
    print(f"多智能体协作房间已启动： http://127.0.0.1:{PORT}")
    print(f"Obsidian 仓库： {VAULT_DIR}（用 Obsidian 打开这个文件夹即可）")
    print("按 Ctrl+C 停止")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
