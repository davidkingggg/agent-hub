"""本地多智能体协作房间 —— Web GUI 服务。

零第三方依赖，只用标准库。

    python server.py            # 打开 http://127.0.0.1:8898

职责：把 config.json 里的「成员池 + 房间」变成可编辑的界面，
并把 Room 执行过程中的事件通过 SSE 实时推给浏览器。
"""
from __future__ import annotations

import json
import mimetypes
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
import urllib.request

ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(ROOT))

from hub.adapters import build          # noqa: E402
from hub.room import Room               # noqa: E402
from hub.store import Store             # noqa: E402

CONFIG_PATH = ROOT / "config.json"
WEB_DIR = ROOT / "web"
DB_PATH = ROOT / "hub.db"
PORT = int(__import__("os").environ.get("HUB_PORT", "8898"))
# 默认值按平台分流：Windows/codebuddy 一套，POSIX/hermes 一套。
# POSIX 的这两个只是兜底——实际值由 _hermes_info() 从成员 env 里的
# HERMES_HOME/config.yaml 读（model.default / agent.reasoning_effort）。
if os.name == "nt":
    DEFAULT_MODEL = "deepseek-v4.1-flash"
    DEFAULT_EFFORT = "ultracode"
    EFFORTS = ["minimal", "low", "medium", "high", "xhigh", "max", "ultracode"]
    # codebuddy 走 -p 非交互 + stdin 喂 prompt
    DEFAULT_CMD = ("codebuddy -p -y --model {model} --effort {effort} --port {port}")
else:
    DEFAULT_MODEL = "mimo-v2.6-flash"
    DEFAULT_EFFORT = "medium"
    # hermes 的推理档位（--reasoning 的合法取值），和 codebuddy 的 effort 不是一回事
    EFFORTS = ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
    # hermes 走 stdin（--query-file -）拿 prompt，--quiet 只把最终回复写到
    # stdout，--in 显式钉住工作目录（不传时文件工具会解析到 HOME）。
    # {workdir} 占位符由 CliAdapter._exec 在运行时替换成房间工作目录；
    # {model}/{effort} 由 build_room / _save_member 展开成成员自己的值。
    DEFAULT_CMD = ("hermes chat --query-file - --quiet --yolo --in {workdir} "
                   "--model {model} --reasoning {effort}")

_models_cache: list[str] | None = None

# ---------- 安全与健壮性（移植自 codebuddy 线 6ab4fb0） ----------
MAX_BODY = 64 * 1024 * 1024          # 请求体上限，防超大 Content-Length 吃光内存
MAX_RAW = 200 * 1024 * 1024          # 单个文件吐出的上限
# 模型名会被拼进 shell 命令（adapters._with_model），白名单校验
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/:]{0,63}$")
# 成员名：会被拼进 @提及、记忆文件名、glob 模式
MEMBER_RE = re.compile(r"[\w\u4e00-\u9fa5\-]{1,32}")
SKIP_DIRS = {"__pycache__", "node_modules", ".git", ".venv", "venv", ".idea"}

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
        cmd = cmd.replace("{model}", str(spec.get("model") or default_model(cfg)))
        cmd = cmd.replace("{effort}", str(spec.get("effort") or default_effort(cfg)))
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
                verbose=False, on_event=on_event)


# ============================ 项目笔记 ============================

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


def room_workdir(room_id: str) -> Path:
    """房间的共享工作目录（workspaces/<房间>）。"""
    cfg = load_config()
    room = next((r for r in cfg["rooms"] if r["id"] == room_id), None)
    if room is None:
        raise KeyError(f"房间不存在: {room_id}")
    return (ROOT / "workspaces" / room.get("workdir", room_id)).resolve()


def project_path(room_id: str) -> Path:
    cfg = load_config()
    room_cfg = next((r for r in cfg["rooms"] if r["id"] == room_id), None)
    # 房间不存在时不能拿 URL 片段直接拼——`../..` 会跑到项目目录外面去
    wd = room_cfg.get("workdir", room_id) if room_cfg \
        else safe_filename(room_id) or "_unknown"
    return ROOT / "projects" / wd / "project.md"


def memory_path(room_id: str, member: str) -> Path:
    cfg = load_config()
    room_cfg = next((r for r in cfg["rooms"] if r["id"] == room_id), None)
    wd = room_cfg.get("workdir", room_id) if room_cfg \
        else safe_filename(room_id) or "_unknown"
    # member 来自 URL / 请求体，当不可信输入：先剥成纯文件名，
    # 否则 `../../README` 能读写项目目录之外的任意 .md
    safe = safe_filename(member)
    if safe.lower().endswith(".md"):
        safe = safe[:-3]
    if not safe or safe in (".", ".."):
        safe = "_"
    return ROOT / "projects" / wd / "memory" / f"{safe}.md"


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


# ============================ hermes 后端：模型/档位数据源 ============================

# API /models 返回里混着 tts/asr 这类非聊天模型（小米 API 就有 4 个），
# 选了也跑不了聊天，按名字关键词滤掉。
_NONCHAT_TOKENS = ("tts", "asr", "voice", "embedding", "rerank",
                   "whisper", "moderation", "dall", "image")
_api_models_cache: dict = {}          # 上次实时查到的结果 {"ts", "models"}，断网时兜底


def _is_chat_model(mid: str) -> bool:
    s = str(mid).lower()
    return not any(t in s for t in _NONCHAT_TOKENS)


def _read_env_key(path: Path, name: str) -> str:
    """从 hub 自己的 .env 里读一个 key。只读不写，不做任何修改。"""
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == name:
                return v.strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def _hermes_info(cfg: dict) -> dict:
    """解析成员 env 里的 HERMES_HOME，读出 hub 这套 hermes 的关键配置。

    config.yaml 和 .env 是主配置的副本，模型/档位真值以它们为准；
    读不到（Windows、文件缺失、PyYAML 不在）就退回平台兜底常量。
    """
    info = {"home": "", "model": DEFAULT_MODEL, "effort": DEFAULT_EFFORT,
            "base_url": "", "key_env": ""}
    if os.name == "nt":
        return info
    for m in cfg.get("members") or []:
        h = (m.get("env") or {}).get("HERMES_HOME")
        if h:
            info["home"] = h
            break
    if not info["home"]:
        return info
    try:
        import yaml
        y = yaml.safe_load((Path(info["home"]) / "config.yaml").read_text(encoding="utf-8")) or {}
        mc = y.get("model") or {}
        info["model"] = mc.get("default") or info["model"]
        info["effort"] = ((y.get("agent") or {}).get("reasoning_effort")) or info["effort"]
        info["base_url"] = mc.get("base_url") or ""
        prov = str(mc.get("provider") or "")
        if prov.startswith("custom:"):
            want = prov.split(":", 1)[1]
            for p in y.get("custom_providers") or []:
                if p.get("name") == want:
                    info["base_url"] = p.get("base_url") or info["base_url"]
                    info["key_env"] = p.get("key_env") or ""
                    break
    except Exception:
        pass
    return info


def default_model(cfg: dict) -> str:
    return _hermes_info(cfg)["model"]


def default_effort(cfg: dict) -> str:
    return _hermes_info(cfg)["effort"]


def model_catalog(cfg: dict) -> dict:
    """/api/models 的数据源。

    hermes 后端（POSIX）：
      1. 实时查活跃 provider 的 GET /models —— 可选模型以接入的 API 为准；
         非聊天模型（tts/asr…）滤掉，查不通就用上次成功的缓存。
      2. 并上其他 custom_providers 登记过的模型（接入的不止一个源）。
         活跃源已由 API 给出，它在 config 里登记但 API 没列的视为已下线，不再展示。
      3. 保证默认模型一定在列表里。
    Windows/codebuddy：保持原逻辑（codebuddy --help 解析）。
    """
    if os.name == "nt":
        return {"models": available_models(), "default": DEFAULT_MODEL,
                "efforts": EFFORTS, "default_effort": DEFAULT_EFFORT}

    info = _hermes_info(cfg)
    models: list[str] = []
    api_ok = False
    active_name = ""
    registered: list[tuple[str, str]] = []      # [(接入名, 模型id)] 所有接入登记过的
    if info["home"]:
        try:
            import yaml
            y = yaml.safe_load((Path(info["home"]) / "config.yaml").read_text(encoding="utf-8")) or {}
            prov = str((y.get("model") or {}).get("provider") or "")
            if prov.startswith("custom:"):
                active_name = prov.split(":", 1)[1]
            for p in y.get("custom_providers") or []:
                pname = str(p.get("name") or "")
                for mid in (p.get("models") or {}):
                    if _is_chat_model(mid):
                        registered.append((pname, str(mid)))
        except Exception:
            pass

    if info["base_url"] and info["key_env"] and info["home"]:
        key = _read_env_key(Path(info["home"]) / ".env", info["key_env"])
        if key:
            try:
                req = urllib.request.Request(
                    info["base_url"].rstrip("/") + "/models",
                    headers={"Authorization": f"Bearer {key}"})
                with urllib.request.urlopen(req, timeout=8) as r:
                    data = json.loads(r.read().decode("utf-8"))
                ids = [m.get("id") for m in (data.get("data") or data.get("models") or [])
                       if isinstance(m, dict) and m.get("id")]
                ids = [i for i in ids if _is_chat_model(i)]
                if ids:
                    _api_models_cache.update({"ts": time.time(), "models": ids,
                                              "provider": active_name})
                    api_ok = True
                    # 实时列表用「接入名/模型」限定，多接入同名不打架
                    models = [f"{active_name}/{i}" if active_name else i for i in ids]
            except Exception:
                pass
    if not models:
        cached = list(_api_models_cache.get("models") or [])
        if cached:
            cname = str(_api_models_cache.get("provider") or "")
            models = [f"{cname}/{i}" if cname else i for i in cached]
            api_ok = bool(models)

    # 各接入登记过的模型（活跃源实时拿到了就跳过它登记的——API 没列的视为已下线）
    for pname, mid in registered:
        if pname == active_name and api_ok:
            continue
        q = f"{pname}/{mid}" if pname else mid
        if q not in models:
            models.append(q)

    default = info["model"]
    qdefault = f"{active_name}/{default}" if (active_name and default) else default
    if qdefault and qdefault not in models:
        models.insert(0, qdefault)
    return {"models": models, "default": qdefault or default,
            "efforts": EFFORTS, "default_effort": info["effort"]}


# ============================ 设置页：API 接入（读写 HERMES_HOME 原生配置） ============================
#
# GUI 上配的「接入」就是 hermes 自己的 custom_providers：
#   provider 块  -> HERMES_HOME/config.yaml（safe_dump 原序回写 + .bak + 写后自检）
#   API Key      -> HERMES_HOME/.env 的 <NAME>_API_KEY 行（整行替换，其余行原样保留）
# 成员弹窗的模型下拉拿「接入名/模型名」限定格式，多个接入同名模型也不冲突。

def _hub_files(cfg: dict):
    info = _hermes_info(cfg)
    home = Path(info["home"]) if info["home"] else None
    return home, (home / "config.yaml") if home else None, (home / ".env") if home else None


def _load_hub_yaml(cfg: dict):
    _, yml, envf = _hub_files(cfg)
    if not yml or not yml.exists():
        raise RuntimeError("找不到 HERMES_HOME/config.yaml（成员 env 里没配 HERMES_HOME？）")
    import yaml
    data = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
    return data, yml, envf


def _save_hub_yaml(yml: Path, data: dict) -> None:
    """原序回写 + 固定备份 + 写后解析自检（坏了立刻回滚，绝不留半截 YAML）。"""
    import yaml
    backup = yml.with_name(yml.name + ".bak")
    original = yml.read_text(encoding="utf-8")
    backup.write_text(original, encoding="utf-8")
    text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False,
                          default_flow_style=False)
    yml.write_text(text, encoding="utf-8")
    try:
        yaml.safe_load(yml.read_text(encoding="utf-8"))
    except Exception:
        yml.write_text(original, encoding="utf-8")
        raise RuntimeError("config.yaml 写入自检失败，已回滚")


def _key_env_name(name: str) -> str:
    return (re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper() or "API") + "_API_KEY"


def _env_set(envf: Path, key: str, value: str) -> None:
    lines = envf.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True) \
        if envf.exists() else []
    out, done = [], False
    for ln in lines:
        if ln.split("=", 1)[0].strip() == key:
            out.append(f"{key}={value}\n")
            done = True
        else:
            out.append(ln)
    if not done:
        out.append(f"{key}={value}\n")
    envf.write_text("".join(out), encoding="utf-8")
    try:
        envf.chmod(0o600)
    except Exception:
        pass


def _env_del(envf: Path, key: str) -> None:
    if not envf or not envf.exists():
        return
    lines = envf.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    out = [ln for ln in lines if ln.split("=", 1)[0].strip() != key]
    if len(out) != len(lines):
        envf.write_text("".join(out), encoding="utf-8")


def _active_provider_name(data: dict) -> str:
    prov = str((data.get("model") or {}).get("provider") or "")
    return prov.split(":", 1)[1] if prov.startswith("custom:") else ""


def _models_view(raw: dict) -> list[dict]:
    out = []
    for mid, spec in (raw or {}).items():
        try:
            ctx = int((spec or {}).get("context_length") or 128000)
        except Exception:
            ctx = 128000
        out.append({"id": str(mid), "context_length": ctx})
    return out


def providers_view(cfg: dict) -> dict:
    data, _, envf = _load_hub_yaml(cfg)
    active = _active_provider_name(data)
    views = []
    for p in data.get("custom_providers") or []:
        name = str(p.get("name") or "")
        key_env = str(p.get("key_env") or "") or _key_env_name(name)
        key = _read_env_key(envf, key_env) if envf else ""
        views.append({
            "name": name,
            "base_url": str(p.get("base_url") or ""),
            "api_mode": str(p.get("api_mode") or "chat_completions"),
            "key_env": key_env,
            "key_set": bool(key),
            "key_hint": (key[:3] + "…" + key[-4:]) if len(key) >= 10 else ("已配置" if key else ""),
            "models": _models_view(p.get("models") or {}),
            "active": name == active,
        })
    return {"providers": views,
            "active": active,
            "default_model": str((data.get("model") or {}).get("default") or ""),
            "efforts": EFFORTS}


_NAME_RE = re.compile(r"[A-Za-z0-9_\-]{1,40}")
_MODEL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\-/]{0,120}")


def _validate_models(items) -> dict:
    if not isinstance(items, list):
        raise ValueError("models 必须是数组")
    out: dict = {}
    for it in items:
        mid = str((it or {}).get("id") or "").strip()
        if not mid or not _MODEL_ID_RE.fullmatch(mid):
            raise ValueError(f"模型 id 不合法：{mid!r}")
        try:
            ctx = int((it or {}).get("context_length") or 128000)
        except Exception:
            raise ValueError(f"模型 {mid} 的 context_length 必须是数字")
        out[mid] = {"context_length": max(1000, min(ctx, 4_000_000))}
    return out


def save_provider(cfg: dict, body: dict) -> dict:
    name = str(body.get("name") or "").strip()
    base_url = str(body.get("base_url") or "").strip()
    if not _NAME_RE.fullmatch(name or ""):
        raise ValueError("接入名只能是字母/数字/-/_，1~40 位")
    if not base_url.startswith(("http://", "https://")):
        raise ValueError("Base URL 必须以 http(s):// 开头")
    key_env = str(body.get("key_env") or "").strip() or _key_env_name(name)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,60}", key_env):
        raise ValueError("Key 环境变量名不合法")

    data, yml, envf = _load_hub_yaml(cfg)
    provs = data.setdefault("custom_providers", [])
    found = next((p for p in provs if str(p.get("name")) == name), None)
    if found is None:
        found = {"name": name, "api_mode": "chat_completions"}
        provs.append(found)
    found["base_url"] = base_url
    found["api_mode"] = str(found.get("api_mode") or "chat_completions")
    found["key_env"] = key_env
    if body.get("models") is not None:
        found["models"] = _validate_models(body.get("models"))

    api_key = str(body.get("api_key") or "").strip()
    if api_key and envf:
        _env_set(envf, key_env, api_key)
    _save_hub_yaml(yml, data)
    return providers_view(cfg)


def delete_provider(cfg: dict, name: str) -> dict:
    data, yml, envf = _load_hub_yaml(cfg)
    active = _active_provider_name(data)
    if name == active:
        raise ValueError(f"「{name}」是默认接入（model.provider），不能删；"
                         "成员默认模型还依赖它")
    provs = data.get("custom_providers") or []
    found = next((p for p in provs if str(p.get("name")) == name), None)
    if found is None:
        raise ValueError(f"接入不存在：{name}")
    provs.remove(found)
    if envf:
        _env_del(envf, str(found.get("key_env") or "") or _key_env_name(name))
    _save_hub_yaml(yml, data)
    return providers_view(cfg)


def _query_models(base_url: str, key: str) -> list[str]:
    req = urllib.request.Request(base_url.rstrip("/") + "/models",
                                 headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        raw = json.loads(r.read().decode("utf-8"))
    ids = [m.get("id") for m in (raw.get("data") or raw.get("models") or [])
           if isinstance(m, dict) and m.get("id")]
    ids = [i for i in ids if _is_chat_model(i)]
    if not ids:
        raise ValueError("API 没返回可用的聊天模型")
    return ids


def fetch_provider_models(cfg: dict, name: str) -> dict:
    data, _, envf = _load_hub_yaml(cfg)
    p = next((x for x in (data.get("custom_providers") or [])
              if str(x.get("name")) == name), None)
    if p is None:
        raise ValueError(f"接入不存在：{name}")
    key_env = str(p.get("key_env") or "") or _key_env_name(name)
    key = _read_env_key(envf, key_env) if envf else ""
    if not key:
        raise ValueError(f"还没配置 API Key（.env 里的 {key_env} 为空）")
    ids = _query_models(str(p.get("base_url") or ""), key)
    existing = {m["id"]: m["context_length"] for m in _models_view(p.get("models") or {})}
    return {"models": [{"id": i, "context_length": existing.get(i, 128000)} for i in ids]}


def fetch_draft_models(body: dict) -> dict:
    """新增接入（还没保存）时直接用表单里的 URL+Key 查模型。"""
    base_url = str(body.get("base_url") or "").strip()
    key = str(body.get("api_key") or "").strip()
    if not base_url.startswith(("http://", "https://")):
        raise ValueError("Base URL 必须以 http(s):// 开头")
    if not key:
        raise ValueError("先填 API Key 才能获取模型")
    return {"models": [{"id": i, "context_length": 128000}
                       for i in _query_models(base_url, key)]}


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


# ============================ 房间消息队列 ============================
# 同一房间串行执行，不同房间互不影响。
# 为什么不直接并发：Room 内部的 _runs 计数不是线程安全的，
# 两个 say 同时跑会互相踩（同一个成员可能被派两次活）。
# 排队而不是拒绝，是因为「中途想补一句」是正常需求，不该丢消息。

_queues: dict[str, queue.Queue] = {}
_workers: dict[str, threading.Thread] = {}
_running: dict[str, bool] = {}
_queue_lock = threading.Lock()


def enqueue_turn(room_id: str, text: str) -> int:
    """把消息放进房间队列，返回前面还有几条（0 = 立刻开始跑）。

    注意要把「正在跑的那一条」算进去：它已经出队了，但用户眼里它还在前面。
    """
    with _queue_lock:
        q = _queues.setdefault(room_id, queue.Queue())
        ahead = q.qsize() + (1 if _running.get(room_id) else 0)
        q.put(text)
        t = _workers.get(room_id)
        if t is None or not t.is_alive():
            t = threading.Thread(target=_worker_loop, args=(room_id,), daemon=True)
            _workers[room_id] = t
            t.start()
        return ahead


def queue_size(room_id: str) -> int:
    q = _queues.get(room_id)
    return q.qsize() + (1 if _running.get(room_id) else 0) if q else 0


def _worker_loop(room_id: str) -> None:
    q = _queues[room_id]
    while True:
        try:
            text = q.get(timeout=0.6)
        except queue.Empty:
            with _queue_lock:
                # 再确认一次：入队和退出之间有竞态窗口
                if q.empty():
                    _workers.pop(room_id, None)
                    _running.pop(room_id, None)
                    return
            continue

        _running[room_id] = True
        try:
            run_turn(room_id, text)
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
            # 静默返回空字典的话，调用方只会看到「没收到数据」，很难查
            traceback.print_exc()
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
                return self._json(model_catalog(load_config()))
            if parts[1] == "providers":
                try:
                    return self._json(providers_view(load_config()))
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
            if parts[1] == "credits":
                cfg = load_config()
                return self._json(cfg.get("credits") or
                                  {"value": None, "unit": "积分", "updated": None})
            if parts[1] == "rooms" and len(parts) >= 4:
                rid = parts[2]
                if parts[3] == "files" and len(parts) == 4:
                    try:
                        wd = room_workdir(rid)
                    except KeyError as e:
                        return self._json({"error": str(e)}, 404)
                    files = []
                    if wd.exists():
                        # os.walk 带剪枝：深目录和 node_modules 不再整棵遍历
                        for dirpath, dirnames, filenames in os.walk(wd):
                            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
                            for fn in filenames:
                                p = Path(dirpath) / fn
                                try:
                                    st = p.stat()
                                except OSError:
                                    continue
                                files.append({"path": str(p.relative_to(wd)),
                                              "size": st.st_size,
                                              "mtime": int(st.st_mtime)})
                    files.sort(key=lambda f: -f["mtime"])
                    return self._json({"files": files[:500]})
                if parts[3] == "file" and len(parts) >= 5:
                    return self._serve_file(rid, "/".join(parts[4:]))
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
        # 上传走原始字节流（X-Filename 头带名字），不能进 _body() 的 JSON 解析
        if parts[:2] == ["api", "rooms"] and len(parts) >= 4 and parts[3] == "upload":
            return self._upload_file(parts[2])
        body = self._body()
        try:
            # 设置页：API 接入（先匹配 /fetch，别被下面的通用保存抢走）
            # /api/providers/fetch-draft 是 3 段；/api/providers/<name>/fetch 是 4 段
            if parts[:2] == ["api", "providers"] and len(parts) == 3 and parts[2] == "fetch-draft":
                try:
                    return self._json(fetch_draft_models(body))
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
            if parts[:2] == ["api", "providers"] and len(parts) == 4 and parts[3] == "fetch":
                try:
                    return self._json(fetch_provider_models(load_config(), parts[2]))
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
            if parts[:2] == ["api", "providers"] and len(parts) == 2:
                try:
                    return self._json(save_provider(load_config(), body))
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
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
                ahead = enqueue_turn(rid, text)
                return self._json({"ok": True, "queued": ahead,
                                   "pending": queue_size(rid)})
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
            if parts[:2] == ["api", "providers"] and len(parts) == 3:
                try:
                    return self._json(delete_provider(load_config(), parts[2]))
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
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
                # 删房间时清掉它的待处理队列，worker 不再捞死房间的任务
                with _queue_lock:
                    _queues.pop(parts[2], None)
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
        # 名字会被拼进 @提及、记忆文件名、glob 模式，限制成安全字符
        if not MEMBER_RE.fullmatch(name):
            return self._json(
                {"error": "名字只能用中英文、数字、下划线或连字符，最长 32 个字符"}, 400)
        cfg = ensure_ports(load_config())
        old = next((m for m in cfg["members"] if m["name"] == name), None)
        model = (body.get("model") or "").strip() \
            or (old.get("model") if old else "") or default_model(cfg)
        effort = (body.get("effort") or "").strip() \
            or (old.get("effort") if old else "") or default_effort(cfg)
        # model 会被拼进 shell 命令（adapters._with_model），必须白名单
        if not MODEL_RE.fullmatch(model):
            return self._json({"error": f"模型名不合法：{model}"}, 400)
        # effort 同样会被拼进 --reasoning/--effort，只能是合法档位
        if effort not in EFFORTS:
            return self._json({"error": f"推理强度不合法：{effort}"}, 400)

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
            # env（如 HERMES_HOME 隔离目录）不能在保存时丢：UI 表单不带这个
            # 字段，丢了成员就会退回宿主 hermes home，记忆注入白名单全回来。
            "env": body.get("env") if "env" in body
                   else (old.get("env") if old else None),
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
        # 不能用 startswith 判前缀：`web-evil/x` 也以 `web` 开头。
        # 用 parents 判断真实的目录归属。
        if path != root and root not in path.parents:
            path = root / "index.html"
        if not path.is_file():
            path = root / "index.html"
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(path.suffix, "application/octet-stream"))
        # 本地工具：禁缓存，改完前端刷新立刻生效（否则手机浏览器会拿旧版）
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_file(self, room_id: str, rel: str) -> None:
        """工作目录文件查看：文本类直接渲染，二进制走下载。"""
        try:
            wd = room_workdir(room_id)
        except KeyError as e:
            return self._json({"error": str(e)}, 404)
        target = (wd / rel).resolve()
        if not str(target).startswith(str(wd) + os.sep) or not target.is_file():
            return self._json({"error": "文件不存在"}, 404)
        if target.stat().st_size > MAX_RAW:
            # 不整读进内存：超大文件让用户本地打开
            return self._json({"error": f"文件超过 {MAX_RAW // (1024 * 1024)}MB，"
                                        f"请在本地打开"}, 413)
        data = target.read_bytes()
        ctype = mimetypes.guess_type(target.name)[0]
        # 浏览器不认 text/markdown 这类 MIME（会变成下载），统一按 text/plain 展示
        if target.suffix.lower() in (".md", ".markdown", ".log", ".csv",
                                      ".yaml", ".yml", ".toml", ".ini", ".cfg"):
            ctype = "text/plain; charset=utf-8"
        elif ctype is None:
            try:
                data[:4096].decode("utf-8")
                ctype = "text/plain; charset=utf-8"
            except UnicodeDecodeError:
                ctype = "application/octet-stream"
        elif ctype.startswith("text/") and "charset" not in ctype:
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Cache-Control", "no-store")
        if (ctype or "").startswith("application/octet-stream"):
            self.send_header("Content-Disposition",
                             f"attachment; filename=\"{target.name}\"")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _upload_file(self, room_id: str) -> None:
        """PUT /api/rooms/<rid>/upload，原始字节 + X-Filename 头。"""
        try:
            wd = room_workdir(room_id)
        except KeyError as e:
            return self._json({"error": str(e)}, 404)
        name = unquote(self.headers.get("X-Filename") or "").strip().replace("\\", "/")
        if (not name or name.startswith("/") or "\x00" in name
                or ".." in name.split("/")):
            return self._json({"error": "文件名不合法"}, 400)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return self._json({"error": "空文件"}, 400)
        if n > 50 * 1024 * 1024:
            return self._json({"error": "单文件最大 50MB"}, 413)
        data = self.rfile.read(n)
        target = wd / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return self._json({"ok": True, "path": name, "size": len(data)})


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
