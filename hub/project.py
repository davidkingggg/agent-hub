"""给群聊推断一个「项目目录名」。

用户要的是：记忆按项目存，目录名一看就知道这个群在做什么。
所以名字优先取自**项目背景**，其次取自这个群的**第一条用户消息**，最后退回房间名。

三条纪律：

  1. 定下来就写进 config.json，之后不再自动变——目录名一变，记忆就散了
  2. 看着像密钥的内容一律不拿来命名（有群第一条消息就是 SecretId/SecretKey）
  3. 结果必须是合法目录名：去掉 Windows 非法字符和路径分隔符
"""
from __future__ import annotations

import re
from pathlib import Path

from . import vault

MAX_LEN = 20

# 出现这些词就不拿它当名字，宁可退回房间名
SENSITIVE = (
    "secretid", "secretkey", "accesskey", "access_key", "password", "passwd",
    "token", "apikey", "api_key", "api key", "private key", "私钥",
    "密码", "密钥", "口令", "验证码",
)

# 中文需求常见的开场白，去掉之后剩下的才是「做什么」
STRIP_PREFIX = (
    "帮我看看", "帮我看一下", "帮我查一下", "帮我", "帮忙",
    "我要", "我想", "我需要", "我们要",
    "麻烦", "请帮我", "请你", "请",
    "看看", "查一下", "了解一下", "调研一下",
)

_AT_RE = re.compile(r"@[\w\u4e00-\u9fa5\-]+")
_URL_RE = re.compile(r"https?://\S+")
_MD_RE = re.compile(r"[#*`>\[\]{}()（）【】「」《》\"'“”‘’]")
_SENT_RE = re.compile(r"[，。、；：！？,.;:!?\n\r]")
_ILLEGAL_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_SPACE_RE = re.compile(r"\s+")

# 这些名字等于「没起名」，不能拿来当项目目录名
PLACEHOLDER_NAMES = {"新群聊", "未命名", "未命名群聊", "新项目", ""}


def is_placeholder(name: str, room_id: str = "") -> bool:
    n = (name or "").strip()
    return not n or n in PLACEHOLDER_NAMES or n == room_id


def looks_sensitive(text: str) -> bool:
    low = (text or "").lower()
    return any(w in low for w in SENSITIVE)


def first_line(text: str) -> str:
    """项目背景可能有多行，只取第一行正文（跳过 `>` 引用块）。"""
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if ln and not ln.startswith(">"):
            return ln
    return ""


def clean(text: str, limit: int = MAX_LEN) -> str:
    """清洗成能当目录名的短名字。"""
    t = _AT_RE.sub(" ", text or "")
    t = _URL_RE.sub(" ", t)
    t = _MD_RE.sub(" ", t)
    # 只取第一个分句：项目名不需要一整句话
    t = _SENT_RE.split(t)[0]
    t = _SPACE_RE.sub(" ", t).strip()

    for p in STRIP_PREFIX:
        if t.startswith(p):
            t = t[len(p):].strip()
            break

    t = _ILLEGAL_RE.sub("", t)
    return t[:limit].strip(" .-_")


def suggest(room_name: str, background: str = "", first_message: str = "") -> str:
    """按优先级挑一个名字：项目背景 > 第一条用户消息 > 房间名。"""
    for cand in (first_line(background), first_message):
        cand = (cand or "").strip()
        if not cand or looks_sensitive(cand):
            continue
        name = clean(cand)
        if len(name) >= 2:
            return name
    return clean(room_name) or "未命名项目"


def unique(base: str, taken: set[str]) -> str:
    """重名加序号，别让两个项目写进同一个目录。"""
    if base not in taken:
        return base
    i = 2
    while f"{base}-{i}" in taken:
        i += 1
    return f"{base}-{i}"


def room_id_of(project_md: Path) -> str:
    """从 project.md 的 frontmatter 里读 room_id。

    这是「目录 ↔ 房间」唯一的锚点：目录名可以随便改，room_id 不会变。
    """
    if not project_md.exists():
        return ""
    try:
        text = project_md.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    front, _ = vault.split_front(text)
    m = re.search(r"^room_id:\s*(.+)$", front, re.M)
    if not m:
        return ""
    return m.group(1).strip().strip('"').strip("'")


def adopt_names_from_disk(cfg: dict, vault_root: Path) -> bool:
    """在 Obsidian 里手动改文件夹名，以磁盘为准同步回 config。

    没有这一步的话，用户在 Obsidian 里改了名，程序还按 config 里的旧名字
    把目录重建出来，记忆就分叉了。
    """
    rooms = {r.get("id"): r for r in (cfg.get("rooms") or []) if r.get("id")}
    if not rooms or not vault_root.is_dir():
        return False

    found: dict[str, list[tuple[float, str]]] = {}
    for d in vault_root.iterdir():
        if not d.is_dir() or d.name.startswith("."):
            continue
        pm = d / "project.md"
        rid = room_id_of(pm)
        if not rid or rid not in rooms:
            continue
        try:
            mtime = pm.stat().st_mtime
        except OSError:
            mtime = 0.0
        found.setdefault(rid, []).append((mtime, d.name))

    changed = False
    for rid, items in found.items():
        room = rooms[rid]
        names = [n for _, n in items]
        if room.get("project") in names:
            continue                       # 没改名（或改名后又同步回来了）
        items.sort(reverse=True)           # 同一房间有多个目录时，认最新改的那个
        room["project"] = items[0][1]
        changed = True

    # 两个房间指向同一个目录时，后来者加序号，别写进同一份记忆
    used: set[str] = set()
    for r in cfg.get("rooms") or []:
        name = r.get("project")
        if not name:
            continue
        if name in used:
            name = unique(name, used)
            r["project"] = name
            changed = True
        used.add(name)

    return changed


def assign_names(cfg: dict, vault_root: Path, first_message) -> bool:
    """给还没定名的群聊定项目名，并把旧目录里的记忆并过去。

    还没有任何内容的群聊先跳过——等它真开始干活，再按任务内容取名。
    返回是否有改动（调用方据此决定要不要写回 config.json）。
    """
    # 先对齐磁盘：用户可能刚在 Obsidian 里改过文件夹名
    changed = adopt_names_from_disk(cfg, vault_root)

    rooms = cfg.get("rooms") or []
    taken = {vault.room_dir(r) for r in rooms if r.get("project")}
    # 磁盘上已有的目录也算占用：否则两个群聊的第一条消息一样，就会写进同一个目录
    if vault_root.is_dir():
        taken |= {d.name for d in vault_root.iterdir() if d.is_dir()}

    for r in rooms:
        if r.get("project"):
            continue
        rid = r.get("id", "")
        old_dir = vault_root / (r.get("workdir") or rid)
        bg = vault.read_section(old_dir / "project.md", vault.SECTION_BG)
        msg = (first_message(rid) or "").strip()
        # 既没内容、名字又是默认值 → 等它真开始干活再定名
        if not bg.strip() and not msg and is_placeholder(r.get("name"), rid):
            continue

        name = unique(suggest(r.get("name") or rid, bg, msg), taken)
        r["project"] = name
        taken.add(name)
        changed = True
        vault.adopt_dir(old_dir, vault_root / name)

    return changed
