"""Obsidian vault：把「成员记忆」和「项目记忆」写成能进双链图谱的笔记。

`projects/` 目录本身就是 vault 根。三个使用方要同时满意：

  1. Obsidian —— 要 frontmatter、要 `[[双链]]`、要能进图谱
  2. 程序     —— 要稳定的追加格式，读的时候不能把 YAML 塞进 prompt
  3. 人       —— 直接用编辑器打开也得看得懂

所以格式固定成：

    ---
    frontmatter（type / room / member / tags / updated）
    ---
    # 标题

    > 一句说明
    > 相关：[[...]]

    ## 记忆          ← 成员记忆
    - [10-29 20:40] ...

或者项目笔记：

    ## 项目背景      ← 人在界面里编辑的部分
    ...
    ## 对话记录      ← 程序自动追加的部分

程序只读「记忆」「项目背景」「对话记录」这三个区块的内容，frontmatter 和标题永不进 prompt。
两个区块都固定放在文件末尾，所以追加就是往文件尾部写，不需要重写整个文件。
"""
from __future__ import annotations

import re
import time
from pathlib import Path

FM_RE = re.compile(r"\A---\r?\n.*?\r?\n---[ \t]*\r?\n?", re.S)
H2_RE = re.compile(r"^##\s+(.+?)\s*$")

SECTION_MEMORY = "记忆"
SECTION_BG = "项目背景"
SECTION_LOG = "对话记录"

INDEX_NAME = "_index.md"
SKIP_DIRS = {"memory"}


# ============================ frontmatter ============================

def _yaml_val(v) -> str:
    s = str(v)
    if not s:
        return '""'
    # ISO 时间戳不加引号，Obsidian 才会认成日期属性而不是纯文本
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?", s):
        return s
    if (re.search(r'[:#\[\]{}&*!|>%@`,]', s) or s != s.strip()
            or s.lower() in ("true", "false", "null", "yes", "no", "~")):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def build_front(meta: dict) -> str:
    lines = ["---"]
    for k, v in meta.items():
        if isinstance(v, (list, tuple)):
            if not v:
                lines.append(f"{k}: []")
                continue
            lines.append(f"{k}:")
            for item in v:
                lines.append(f"  - {_yaml_val(item)}")
        else:
            lines.append(f"{k}: {_yaml_val(v)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def split_front(text: str) -> tuple[str, str]:
    """拆成 (frontmatter 原文含分隔符, 正文)。没有 frontmatter 就返回 ("", 全文)。"""
    m = FM_RE.match(text or "")
    if not m:
        return "", text or ""
    return m.group(0), (text or "")[m.end():]


def touch_updated(path: Path, stamp: str | None = None) -> None:
    """只改 frontmatter 里那一行 updated，不动正文。"""
    if not path.exists():
        return
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return
    front, body = split_front(text)
    if not front:
        return
    stamp = stamp or time.strftime("%Y-%m-%dT%H:%M")
    new_front = re.sub(r"^updated:.*$", f"updated: {_yaml_val(stamp)}",
                       front, count=1, flags=re.M)
    if new_front != front:
        path.write_text(new_front + body, encoding="utf-8")


def update_front(path: Path, fields: dict) -> bool:
    """改写 frontmatter 里已存在的字段。只替换、不新增，避免把文件写坏。

    用于项目改名后同步 `project` / `room` 这类跟着变的值。
    """
    if not path.exists() or not fields:
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    front, body = split_front(text)
    if not front:
        return False

    new = front
    for k, v in fields.items():
        new = re.sub(rf"^{re.escape(k)}:.*$", f"{k}: {_yaml_val(v)}",
                     new, count=1, flags=re.M)
    if new == front:
        return False
    path.write_text(new + body, encoding="utf-8")
    return True


# ============================ 区块解析 ============================

def read_section(path: Path, section: str, limit_lines: int | None = None,
                 limit_chars: int | None = None, to_end: bool = False) -> str:
    """读某个 `## 区块` 的内容。

    `to_end=True` 表示这个区块一直延伸到文件末尾——对话记录必须这么读：
    agent 的输出里经常有 `## 小标题`，按 `##` 切会把后半段记录切丢。
    """
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""

    _, body = split_front(text)
    lines = body.splitlines()

    start = None
    for i, ln in enumerate(lines):
        m = H2_RE.match(ln)
        if m and m.group(1) == section:
            start = i + 1
            break
    if start is None:
        return ""

    end = len(lines)
    if not to_end:
        fence = False
        for j in range(start, len(lines)):
            if lines[j].lstrip().startswith("```"):
                fence = not fence
            if not fence and H2_RE.match(lines[j]):
                end = j
                break

    chunk = "\n".join(lines[start:end]).strip()
    if limit_lines:
        ls = [ln for ln in chunk.splitlines() if ln.strip()]
        return "\n".join(ls[-limit_lines:])
    if limit_chars and len(chunk) > limit_chars:
        cut = len(chunk) - limit_chars
        # 从中间截断会把首行切一半，而且第一段会失去「谁说的」，
        # 所以往回找到最近的条目标题再截
        head = chunk.rfind("\n### ", 0, cut)
        if head != -1:
            cut = head + 1
        else:
            nl = chunk.find("\n", cut)
            cut = nl + 1 if nl != -1 else cut
        return chunk[cut:].lstrip("\n")
    return chunk


def write_section(path: Path, section: str, content: str) -> None:
    """整段替换某个区块，其余区块和 frontmatter 原样保留。"""
    text = path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""
    front, body = split_front(text)
    lines = body.splitlines()

    start = None
    for i, ln in enumerate(lines):
        m = H2_RE.match(ln)
        if m and m.group(1) == section:
            start = i
            break

    block = [f"## {section}", ""] + (content.rstrip().splitlines() if content.strip() else []) + [""]
    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(block)
    else:
        end = len(lines)
        for j in range(start + 1, len(lines)):
            if H2_RE.match(lines[j]):
                end = j
                break
        lines = lines[:start] + block + lines[end:]

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(front + "\n".join(lines).rstrip() + "\n", encoding="utf-8")


def refresh_intro(path: Path, title: str, intro: str) -> bool:
    """只重写 `# 标题` 到第一个 `## 区块` 之间的导航区。

    项目改名后，旧的双链会指向不存在的目录，所以每次同步都要刷一遍。
    导航区归程序管，正文区块一个字都不动。
    """
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8", errors="ignore")
    front, body = split_front(text)
    m = re.search(r"^## ", body, re.M)
    if not m:
        return False
    new_body = f"# {title}\n\n{intro}\n\n" + body[m.start():]
    if new_body == body:
        return False
    path.write_text(front + new_body, encoding="utf-8")
    return True


def adopt_dir(old: Path, new: Path) -> None:
    """把旧目录的内容并进新目录。项目改名后，记忆要跟着目录走。"""
    if not old.exists() or old.resolve() == new.resolve():
        return
    new.mkdir(parents=True, exist_ok=True)
    for item in list(old.iterdir()):
        target = new / item.name
        if item.is_dir():
            adopt_dir(item, target)
            try:
                item.rmdir()
            except OSError:
                pass
        elif not target.exists():
            item.replace(target)
        else:
            # 两边都有同名文件：留着旧的，新来的加序号
            i = 2
            while (new / f"{item.stem}-{i}{item.suffix}").exists():
                i += 1
            item.replace(new / f"{item.stem}-{i}{item.suffix}")
    try:
        old.rmdir()
    except OSError:
        pass


def append_block(path: Path, section: str, block: str) -> None:
    """往文件尾部追加。所有区块都在末尾，所以等价于追加到最后一个区块。"""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"## {section}\n", encoding="utf-8")
    text = path.read_text(encoding="utf-8", errors="ignore")
    if f"## {section}" not in text:           # 兜底：区块被删了就补回来
        text = text.rstrip() + f"\n\n## {section}\n"

    body = text.rstrip()
    last = body.splitlines()[-1] if body.splitlines() else ""
    # 紧跟在区块标题下面时留一个空行，Obsidian 里看着才不挤；
    # 往列表里续行时只留一个换行，免得每条记忆之间空一大截。
    sep = "\n\n" if H2_RE.match(last) else "\n"
    path.write_text(body + sep + block.rstrip() + "\n", encoding="utf-8")


# ============================ 删除对话记录 ============================

def _entry_matches(chunk: list[str], id_set: set[str], fallback: set) -> bool:
    text = "\n".join(chunk)
    m = re.search(r"<!--\s*msg:(\d+)\s*-->", text)
    if m:
        return m.group(1) in id_set
    m2 = re.match(r"^###\s+\[([^\]]+)\]\s+(.+?)\s*$", chunk[0])
    if not m2:
        return False
    return (m2.group(1).strip(), m2.group(2).strip()) in fallback


def remove_log_entries(path: Path, keys: list[dict]) -> int:
    """从 `## 对话记录` 里删掉指定条目，返回删掉的条数。

    新记录带 `<!-- msg:ID -->` 标记，按 id 精确匹配；
    老记录没有标记，退回「时间 + 发送者」匹配——同一分钟同一人发两条会误伤，
    但这种概率很低，而且删错了还能从 .trash 里翻回来。
    """
    if not path.exists() or not keys:
        return 0
    text = path.read_text(encoding="utf-8", errors="ignore")
    front, body = split_front(text)
    lines = body.splitlines()

    start = None
    for i, ln in enumerate(lines):
        m = H2_RE.match(ln)
        if m and m.group(1) == SECTION_LOG:
            start = i
            break
    if start is None:
        return 0
    # 对话记录是最后一个区块，一路到文件末尾——中间那些 `## 小标题`
    # 是 agent 输出的一部分，不是区块边界，不能拿它们切断
    end = len(lines)

    id_set = {str(k["id"]) for k in keys if k.get("id")}
    fallback = {(k.get("stamp"), k.get("sender")) for k in keys
                if k.get("stamp") and k.get("sender")}

    kept: list[str] = []
    removed = 0
    chunk: list[str] = []
    for ln in lines[start + 1:end]:
        if re.match(r"^###\s+", ln):
            if chunk:
                if _entry_matches(chunk, id_set, fallback):
                    removed += 1
                else:
                    kept.extend(chunk)
            chunk = [ln]
        else:
            chunk.append(ln)
    if chunk:
        if _entry_matches(chunk, id_set, fallback):
            removed += 1
        else:
            kept.extend(chunk)

    if not removed:
        return 0

    new_lines = lines[:start + 1] + kept + lines[end:]
    path.write_text(front + "\n".join(new_lines).rstrip() + "\n", encoding="utf-8")
    return removed


# ============================ 建笔记 ============================

def _member_link(workdir: str, member: str, label: str) -> str:
    """跨房间有同名成员，所以双链必须带路径，否则 Obsidian 会指错。"""
    return f"[[{workdir}/memory/{member}|{label}]]"


def room_dir(room: dict) -> str:
    """项目目录名。有 project 字段就用它，否则退回 workdir / 房间 id。"""
    return (room.get("project") or room.get("workdir")
            or room.get("id") or "未命名项目")


def _sync_meta(path: Path, meta: dict, title: str, intro: str) -> None:
    """已存在的笔记：刷新导航 + 跟着改的 frontmatter 字段。正文一律不动。"""
    refresh_intro(path, title, intro)
    update_front(path, {"project": meta.get("project", ""),
                        "room": meta.get("room", "")})


def ensure_memory(path: Path, meta: dict, title: str, intro: str) -> None:
    """成员记忆：没有就建，老格式（纯 `- [..]` 行）顺手升级成带 frontmatter 的。"""
    if path.exists() and path.stat().st_size:
        text = path.read_text(encoding="utf-8", errors="ignore")
        if text.startswith("---"):
            _sync_meta(path, meta, title, intro)   # 已经是新格式，只刷导航和元信息
            return
        old = text.strip()
        path.write_text(
            build_front(meta)
            + f"\n# {title}\n\n{intro}\n\n## {SECTION_MEMORY}\n"
            + (old + "\n" if old else ""),
            encoding="utf-8")
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        build_front(meta) + f"\n# {title}\n\n{intro}\n\n## {SECTION_MEMORY}\n",
        encoding="utf-8")


def ensure_project(path: Path, meta: dict, title: str, intro: str) -> None:
    """项目笔记：没有就建；老格式把 `## [时间] 谁` 降成 `###`，归到「对话记录」下。"""
    if path.exists() and path.stat().st_size:
        text = path.read_text(encoding="utf-8", errors="ignore")
        if text.startswith("---"):
            _sync_meta(path, meta, title, intro)   # 已经是新格式，只刷导航和元信息
            return
        body = text
        # 去掉老的一级标题
        body = re.sub(r"\A\s*#\s*项目笔记[^\n]*\n", "", body)
        # 老的 `## [10-27 11:00] 用户` 全部降一级
        body = re.sub(r"^##\s+(?=\[)", "### ", body, flags=re.M)
        body = body.strip()
        path.write_text(
            build_front(meta)
            + f"\n# {title}\n\n{intro}\n\n## {SECTION_BG}\n\n## {SECTION_LOG}\n"
            + (("\n" + body + "\n") if body else ""),
            encoding="utf-8")
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        build_front(meta)
        + f"\n# {title}\n\n{intro}\n\n## {SECTION_BG}\n\n## {SECTION_LOG}\n",
        encoding="utf-8")


# ============================ 索引页 ============================

def _room_stats(project: Path) -> str:
    if not project.exists():
        return "—"
    try:
        text = project.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return "—"
    front, _ = split_front(text)
    m = re.search(r"^updated:\s*(.+)$", front, re.M)
    if not m:
        return "—"
    return m.group(1).strip().strip('"').replace("T", " ")


def rebuild_index(vault_root: Path, rooms: list[dict], members: list[dict]) -> None:
    """重写 `_index.md`。程序维护，手改会被覆盖——这点写在文件里了。"""
    pool = {m["name"]: m for m in members}
    lines = [
        build_front({
            "type": "agent-hub-index",
            "tags": ["agent-hub/index"],
            "updated": _stamp(),
        }),
        "# Agent Hub 记忆库",
        "",
        "> 这个目录就是 Obsidian 仓库根。用 Obsidian「打开文件夹作为仓库」选它即可。",
        "> 本页由程序自动维护，手改会在下次同步时被覆盖。",
        "",
        "## 项目",
        "",
    ]

    total_members = 0
    for r in sorted(rooms, key=lambda x: bool(x.get("archived"))):
        wd = room_dir(r)
        room_name = r.get("name") or wd
        flag = "（已归档）" if r.get("archived") else ""
        lines.append(f"### {wd}{flag}")
        lines.append("")
        lines.append(f"- 群聊：{room_name}")
        lines.append(f"- 项目记忆：[[{wd}/project|项目笔记]]")
        names = r.get("members") or []
        total_members += len(names)
        if names:
            links = []
            for n in names:
                spec = pool.get(n) or {}
                label = spec.get("persona") or n
                links.append(_member_link(wd, n, label))
            lines.append(f"- 成员：{'、'.join(links)}")
        else:
            lines.append("- 成员：（还没有）")
        lines.append(f"- 最近更新：{_room_stats(vault_root / wd / 'project.md')}")
        lines.append("")

    lines += [
        "## 统计",
        "",
        f"- 项目 {len(rooms)} 个",
        f"- 成员记忆 {total_members} 份",
        "",
        "## 怎么用",
        "",
        "- 点项目名进去，就是那个项目的项目记忆（背景 + 对话记录）",
        "- 点成员名跳到他在这个项目里的私有记忆——每个项目各一份，人格设定不在仓库里（在 `config.json`）",
        "- 想按标签筛：`tag:#agent-hub/memory`",
        "",
    ]

    vault_root.mkdir(parents=True, exist_ok=True)
    (vault_root / INDEX_NAME).write_text("\n".join(lines), encoding="utf-8")


# ============================ 统一入口 ============================

def _stamp() -> str:
    """frontmatter 用 ISO 格式，Obsidian 才认成日期属性。"""
    return time.strftime("%Y-%m-%dT%H:%M")


def project_meta(room: dict) -> dict:
    return {
        "type": "agent-hub-project",
        "room": room.get("name") or room.get("id", ""),
        "room_id": room.get("id", ""),
        "project": room_dir(room),
        "leader": room.get("leader") or "",
        "tags": ["agent-hub/project"],
        "updated": _stamp(),
    }


def memory_meta(room: dict, member: dict) -> dict:
    room_name = room.get("name") or room.get("id", "")
    return {
        "type": "agent-hub-memory",
        "room": room_name,
        "room_id": room.get("id", ""),
        "project": room_dir(room),
        "member": member.get("name", ""),
        "persona": member.get("persona") or member.get("name", ""),
        "role": member.get("role", ""),
        "tags": ["agent-hub/memory", f"agent-hub/room/{room_name}"],
        "updated": _stamp(),
    }


def project_intro(room: dict, members: list[dict], dir_name: str) -> str:
    room_name = room.get("name") or room.get("id", "")
    if members:
        links = "、".join(
            _member_link(dir_name, m["name"], m.get("persona") or m["name"])
            for m in members)
        who = f"成员记忆：{links}"
    else:
        who = "成员记忆：（还没有成员）"
    return (f"> 群聊「{room_name}」的项目记忆，全群共享。"
            f"项目背景在界面「项目内容」里编辑。\n"
            f"> {who}")


def memory_intro(room: dict, member: dict, dir_name: str) -> str:
    room_name = room.get("name") or room.get("id", "")
    return (f"> {member.get('persona') or member.get('name')} 在群聊「{room_name}」的私有记忆。"
            f"换一个群聊就是另一份。\n"
            f"> 所属项目：[[{dir_name}/project|{dir_name} · 项目]]　"
            f"人格设定在 `config.json`，不在这里。")


def _migrate_orphan(d: Path) -> None:
    """config 里已经删掉、但磁盘上还留着的历史目录：只做格式升级，不动内容。"""
    fake = {"id": d.name, "name": d.name}
    intro = f"> 历史遗留目录，`{d.name}` 已经不在 `config.json` 里了。"

    proj = d / "project.md"
    if proj.exists():
        ensure_project(proj, project_meta(fake), d.name, intro)

    memdir = d / "memory"
    if memdir.is_dir():
        for m in sorted(memdir.glob("*.md")):
            spec = {"name": m.stem, "persona": m.stem}
            ensure_memory(m, memory_meta(fake, spec), f"{m.stem} · {d.name}", intro)


def sync_vault(vault_root: Path, cfg: dict) -> None:
    """把所有房间的笔记补齐 + 重建索引。启动时和配置变更后调用。"""
    rooms = cfg.get("rooms") or []
    members = cfg.get("members") or []
    pool = {m["name"]: m for m in members}

    for r in rooms:
        wd = room_dir(r)
        if not wd:
            continue
        specs = [pool[n] for n in (r.get("members") or []) if n in pool]
        proj = vault_root / wd / "project.md"
        ensure_project(proj, project_meta(r), wd, project_intro(r, specs, wd))
        for spec in specs:
            ensure_memory(
                vault_root / wd / "memory" / f"{spec['name']}.md",
                memory_meta(r, spec),
                f"{spec.get('persona') or spec['name']} · {wd}",
                memory_intro(r, spec, wd))

    # 配置里没有的目录也升级一下格式，否则打开 vault 会看到两种风格
    if vault_root.is_dir():
        known = {room_dir(r) for r in rooms}
        for d in sorted(vault_root.iterdir()):
            if d.is_dir() and not d.name.startswith(".") and d.name not in known:
                _migrate_orphan(d)

    rebuild_index(vault_root, rooms, members)
