"""房间与消息的持久化。一个房间就是一条共享对话流，所有成员读写同一份。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
    id TEXT PRIMARY KEY, name TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id TEXT, sender TEXT, content TEXT, kind TEXT, created_at REAL,
    meta TEXT);
CREATE INDEX IF NOT EXISTS idx_msg_room ON messages(room_id, id);
"""


class Store:
    def __init__(self, db_path: str | Path):
        p = Path(db_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        # 成员是并发执行的，连接必须跨线程复用，写操作自己加锁
        self.conn = sqlite3.connect(str(p), check_same_thread=False)
        self.lock = threading.Lock()
        self.conn.row_factory = sqlite3.Row
        # WAL：读不挡写、写不挡读。默认的 rollback journal 下，
        # 一个成员在写记忆时另一个成员的 history 查询会被卡住。
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error:
            pass
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """老库补 meta 列。CREATE TABLE IF NOT EXISTS 不会给已存在的表加字段。"""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(messages)")}
        if "meta" not in cols:
            self.conn.execute("ALTER TABLE messages ADD COLUMN meta TEXT")

    def ensure_room(self, name: str = "main") -> str:
        with self.lock:
            row = self.conn.execute("SELECT id FROM rooms WHERE name=?", (name,)).fetchone()
            if row:
                return row["id"]
            self.conn.execute("INSERT INTO rooms VALUES (?,?,?)", (name, name, time.time()))
            self.conn.commit()
            return name

    def add(self, room_id: str, sender: str, content: str, kind: str = "say",
            meta: dict | None = None) -> int:
        """meta 存附件这类结构化附加信息，正文保持原样。"""
        blob = json.dumps(meta, ensure_ascii=False) if meta else None
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO messages (room_id,sender,content,kind,created_at,meta)"
                " VALUES (?,?,?,?,?,?)",
                (room_id, sender, content, kind, time.time(), blob))
            self.conn.commit()
            return cur.lastrowid

    def history(self, room_id: str, limit: int = 40) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM messages WHERE room_id=? ORDER BY id DESC LIMIT ?",
                (room_id, limit)).fetchall()
            out = []
            for r in reversed(rows):
                m = dict(r)
                raw = m.pop("meta", None)
                try:
                    m["meta"] = json.loads(raw) if raw else None
                except (TypeError, ValueError):
                    m["meta"] = None
                out.append(m)
            return out

    def delete_messages(self, room_id: str, ids: list[int]) -> list[dict]:
        """按 id 删消息，返回被删掉的那些——调用方要拿去做 Obsidian 同步。"""
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        with self.lock:
            rows = self.conn.execute(
                f"SELECT * FROM messages WHERE room_id=? AND id IN ({marks})",
                (room_id, *ids)).fetchall()
            if not rows:
                return []
            self.conn.execute(
                f"DELETE FROM messages WHERE room_id=? AND id IN ({marks})",
                (room_id, *ids))
            self.conn.commit()
            out = []
            for r in rows:
                m = dict(r)
                raw = m.pop("meta", None)
                try:
                    m["meta"] = json.loads(raw) if raw else None
                except (TypeError, ValueError):
                    m["meta"] = None
                out.append(m)
            return out

    def transcript(self, room_id: str, limit: int = 20, max_chars: int = 300) -> str:
        lines = []
        for m in self.history(room_id, limit):
            body = m["content"].replace("\n", " ")
            # 附件必须出现在上下文里，否则成员不知道用户传了东西、放在哪
            atts = ((m.get("meta") or {}).get("attachments") or [])
            extra = ""
            if atts:
                names = "、".join(a.get("path") or a.get("name") or "" for a in atts)
                extra = f" ［附件：{names}］"
            lines.append(f"[{m['sender']}] {body[:max_chars]}{extra}")
        return "\n".join(lines) if lines else "（房间刚建立，还没有对话）"
