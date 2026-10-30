"""房间与消息的持久化。一个房间就是一条共享对话流，所有成员读写同一份。"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
    id TEXT PRIMARY KEY, name TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id TEXT, sender TEXT, content TEXT, kind TEXT, created_at REAL);
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
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def ensure_room(self, name: str = "main") -> str:
        with self.lock:
            row = self.conn.execute("SELECT id FROM rooms WHERE name=?", (name,)).fetchone()
            if row:
                return row["id"]
            self.conn.execute("INSERT INTO rooms VALUES (?,?,?)", (name, name, time.time()))
            self.conn.commit()
            return name

    def add(self, room_id: str, sender: str, content: str, kind: str = "say") -> int:
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO messages (room_id,sender,content,kind,created_at) VALUES (?,?,?,?,?)",
                (room_id, sender, content, kind, time.time()))
            self.conn.commit()
            return cur.lastrowid

    def history(self, room_id: str, limit: int = 40) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM messages WHERE room_id=? ORDER BY id DESC LIMIT ?",
                (room_id, limit)).fetchall()
            return [dict(r) for r in reversed(rows)]

    def transcript(self, room_id: str, limit: int = 20, max_chars: int = 300) -> str:
        lines = []
        for m in self.history(room_id, limit):
            body = m["content"].replace("\n", " ")
            lines.append(f"[{m['sender']}] {body[:max_chars]}")
        return "\n".join(lines) if lines else "（房间刚建立，还没有对话）"
