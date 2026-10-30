"""把记忆文件迁移 / 修复成 Obsidian 笔记格式。

平时不需要手动跑——`server.py` 启动时会自动 `sync_vault()`。
这个脚本用于：

  1. 老版本的纯 `- [10-27 11:00] xxx` 记忆文件升级成带 frontmatter 的笔记
  2. 手改坏了 frontmatter 之后重新规范化
  3. 检查 vault 里有哪些文件、链接有没有指错

    python tools/migrate_vault.py          # 迁移 + 重建索引
    python tools/migrate_vault.py --check  # 只看，不改
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hub import vault                   # noqa: E402
from hub import project as proj_name    # noqa: E402

VAULT = ROOT / "projects"
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")
UPDATED_RE = re.compile(r"^updated:\s*(.+)$", re.M)
# 二级标题紧跟正文时补一个空行，Obsidian 里读着不挤
TIGHT_HEADING_RE = re.compile(r"(^## [^\n]+\n)(?=[^\n])", re.M)


def first_message(room_id: str) -> str:
    db = ROOT / "hub.db"
    if not db.exists():
        return ""
    try:
        conn = sqlite3.connect(str(db))
        row = conn.execute(
            "SELECT content FROM messages WHERE room_id=? AND kind='say'"
            " ORDER BY id LIMIT 1", (room_id,)).fetchone()
        conn.close()
        return (row[0] or "") if row else ""
    except sqlite3.Error:
        return ""


def normalize_updated(path: Path) -> bool:
    """把 `updated: "2026-10-29 20:50"` 这种带引号的写法改成 ISO。

    Obsidian 只有认成 ISO 才会把 updated 当日期属性，才能按时间排序/查询。
    """
    text = path.read_text(encoding="utf-8", errors="ignore")
    front, body = vault.split_front(text)
    if not front:
        return False

    m = UPDATED_RE.search(front)
    if not m:
        return False

    raw = m.group(1).strip().strip('"').strip("'").replace("T", " ")
    iso = ""
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            iso = datetime.strptime(raw, fmt).strftime("%Y-%m-%dT%H:%M")
            break
        except ValueError:
            continue
    if not iso or ISO_RE.fullmatch(m.group(1).strip()):
        return False

    path.write_text(UPDATED_RE.sub(f"updated: {iso}", front, count=1) + body,
                    encoding="utf-8")
    return True


def normalize_spacing(path: Path) -> bool:
    """`## 记忆` 下面直接跟内容会显得挤，补一个空行。"""
    text = path.read_text(encoding="utf-8", errors="ignore")
    out = TIGHT_HEADING_RE.sub(r"\1\n", text)
    if out == text:
        return False
    path.write_text(out, encoding="utf-8")
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description="迁移记忆文件为 Obsidian 笔记")
    ap.add_argument("--check", action="store_true", help="只检查，不写文件")
    args = ap.parse_args()

    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

    if args.check:
        print(f"仓库根：{VAULT}")
        rooms = cfg.get("rooms") or []
        for r in rooms:
            name = vault.room_dir(r)
            proj = VAULT / name / "project.md"
            mem_dir = VAULT / name / "memory"
            mem = sorted(mem_dir.glob("*.md")) if mem_dir.is_dir() else []
            flag = "" if r.get("project") else "  ← 还没定名"
            print(f"  {name:<24} 项目笔记={'有' if proj.exists() else '缺'}"
                  f"  成员记忆 {len(mem)} 份{flag}")
        known = {vault.room_dir(r) for r in rooms}
        orphans = [d.name for d in VAULT.iterdir()
                   if d.is_dir() and not d.name.startswith(".") and d.name not in known]
        if orphans:
            print(f"  历史遗留目录：{', '.join(orphans)}")
        return

    # 先定项目目录名（会顺手把 r1714593 这类旧目录并到项目名下）
    if proj_name.assign_names(cfg, VAULT, first_message):
        (ROOT / "config.json").write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        print("已为群聊分配项目目录名并迁移目录")

    vault.sync_vault(VAULT, cfg)

    fixed = sum(1 for p in VAULT.rglob("*.md") if normalize_updated(p))
    spaced = sum(1 for p in VAULT.rglob("*.md") if normalize_spacing(p))
    files = sorted(VAULT.rglob("*.md"))
    print(f"已迁移 {len(files)} 个文件，规范化时间戳 {fixed} 个，补齐空行 {spaced} 个")
    for r in cfg.get("rooms") or []:
        print(f"  {r.get('name') or r.get('id'):<12} → projects/{vault.room_dir(r)}")
    print(f"索引页：{VAULT / vault.INDEX_NAME}")


if __name__ == "__main__":
    main()
