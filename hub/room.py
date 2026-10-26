"""房间：一条共享对话流 + 若干成员 + @ 路由 + 交接。

调度规则刻意做得很笨，因为笨的规则可预测：
  1. 用户发言里 @ 了谁，就并发交给谁；
  2. 没 @ 任何人，交给默认成员；
  3. 某个成员的回复里 @ 了别人，就触发下一轮交接；
  4. 交接层数封顶，防止两个 Agent 互相客气到天亮。
"""
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .adapters import Adapter
from .store import Store

MENTION = re.compile(r"@([\w\u4e00-\u9fa5\-]{1,20})")


class Room:
    def __init__(self, store: Store, room_id: str, members: list[Adapter],
                 workdir: Path, default_member: str = "",
                 max_hops: int = 3, max_runs: int = 1, verbose: bool = True,
                 on_event=None):
        self.store = store
        self.room_id = room_id
        self.members = {m.name: m for m in members}
        self.workdir = Path(workdir).resolve()
        self.workdir.mkdir(parents=True, exist_ok=True)
        # 三块内容按维度分开存：
        #   人格设定 -> config.json（全局，跟着成员走）
        #   项目内容 -> projects/<群聊>/project.md（群聊级，全员共享）
        #   成员记忆 -> projects/<群聊>/memory/<成员>.md（成员 × 群聊）
        self.project_dir = self.workdir.parent.parent / "projects" / self.workdir.name
        self.memory_dir = self.project_dir / "memory"
        self.default = default_member if default_member in self.members \
            else next(iter(self.members), "")
        self.max_hops = max_hops
        self.max_runs = max_runs
        self.verbose = verbose
        self.on_event = on_event
        self._runs: dict[str, int] = {}

    def _emit(self, event: str, **data) -> None:
        if self.on_event:
            try:
                self.on_event(event, data)
            except Exception:
                pass

    def _log(self, msg: str):
        if self.verbose:
            print(msg, flush=True)

    def _targets(self, text: str, strict: bool = False) -> list[str]:
        """从文本里找 @ 到的成员。

        strict=True 用于解析 agent 的回复：只认「行首的 @」，而且只看最后三行。
        否则 agent 在叙述里提一句「同时点名了 @builder」就会被误判成交接。
        """
        if strict:
            lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
            names: list[str] = []
            for ln in lines[-3:]:
                if ln.startswith("@"):
                    names.extend(MENTION.findall(ln))
        else:
            names = MENTION.findall(text)
        return [n for n in dict.fromkeys(names) if n in self.members]

    # ---------- 对外 ----------
    def say(self, text: str, sender: str = "你") -> None:
        self._runs = {}                    # 每轮用户请求重置执行计数
        self.store.add(self.room_id, sender, text)
        self._log(f"\n{sender}: {text}")
        self._emit("message", sender=sender, content=text, kind="say")
        self._append_project("用户", text)
        self._dispatch(text, depth=1)
        self._emit("idle")

    def replay(self, limit: int = 40) -> None:
        for m in self.store.history(self.room_id, limit):
            tag = {"say": "", "result": ""}.get(m["kind"], "")
            self._log(f"\n[{m['sender']}] {m['content']}")

    # ---------- 内部 ----------
    def _dispatch(self, trigger: str, depth: int, parallel: bool = True) -> None:
        if depth > self.max_hops:
            self._log(f"  （已到 {self.max_hops} 层交接上限，停止）")
            return

        targets = self._targets(trigger)
        if not targets:
            if not self.default:
                return
            targets = [self.default]

        # 同一轮里同一个成员最多跑 max_runs 次，避免「用户 @ 了他，别人又交接给他」
        # 导致重复劳动——实测这会让同一个 agent 把同一件事做两遍。
        skipped = [t for t in targets if self._runs.get(t, 0) >= self.max_runs]
        if skipped:
            self._log(f"  （{', '.join(skipped)} 本轮已执行过，跳过）")
        targets = [t for t in targets if self._runs.get(t, 0) < self.max_runs]
        if not targets:
            return
        for t in targets:
            self._runs[t] = self._runs.get(t, 0) + 1

        self._log(f"  → 本轮交给：{', '.join(targets)}")
        self._emit("status", text=f"交给 {', '.join(targets)}")

        results: dict[str, str] = {}
        if parallel and len(targets) > 1:
            with ThreadPoolExecutor(max_workers=min(4, len(targets))) as pool:
                futures = {pool.submit(self._run_one, self.members[t], trigger): t
                           for t in targets}
                for fut in as_completed(futures):
                    name = futures[fut]
                    try:
                        results[name] = fut.result()
                    except Exception as e:
                        results[name] = f"[执行异常] {type(e).__name__}: {e}"
        else:
            # 交接必须串行：下游可能要读上游刚写出来的文件
            for t in targets:
                try:
                    results[t] = self._run_one(self.members[t], trigger)
                except Exception as e:
                    results[t] = f"[执行异常] {type(e).__name__}: {e}"

        for name in targets:                       # 按原顺序落库，保证时间线稳定
            out = (results.get(name) or "").strip()
            if not out:
                continue
            self.store.add(self.room_id, name, out, kind="result")
            preview = out[:280].replace("\n", " ")
            self._log(f"\n{name}: {preview}{'…' if len(out) > 280 else ''}")
            self._emit("message", sender=name, content=out, kind="result")
            self._append_project(name, out)

        for name in targets:                       # 处理交接
            out = results.get(name) or ""
            nxt = [n for n in self._targets(out, strict=True) if n != name]
            for target in nxt:
                self._log(f"  ↪ {name} 交接给 {target}")
                self._dispatch(f"（来自 {name} 的交接）\n{out}", depth + 1, parallel=False)

    def _run_one(self, member: Adapter, trigger: str) -> str:
        ok, why = member.available()
        if not ok:
            return f"[{member.name} 当前不可用] {why}"
        out = member.run(self._prompt(member, trigger), self.workdir)
        # 把自己这次做了什么记进私有记忆，下次还能想起来
        if out and not out.startswith("["):
            gist = " ".join(out.split())[:110]
            task = " ".join(trigger.split())[:70]
            self._append_memory(member, f"任务「{task}」→ {gist}")
        return out

    # ---------- 私有记忆 ----------
    def _memory_path(self, member: Adapter) -> Path:
        """记忆放在工作区之外，队友翻文件也看不到。"""
        return self.memory_dir / f"{member.name}.md"

    def _read_memory(self, member: Adapter) -> str:
        p = self._memory_path(member)
        if not p.exists():
            return "（还没有积累，这是你的第一次任务）"
        lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
        return "\n".join(lines[-25:]) if lines else "（还没有积累）"

    def _append_memory(self, member: Adapter, note: str) -> None:
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%m-%d %H:%M")
        with self._memory_path(member).open("a", encoding="utf-8") as f:
            f.write(f"- [{stamp}] {note}\n")

    def _files(self) -> str:
        items = [str(p.relative_to(self.workdir)) for p in self.workdir.rglob("*") if p.is_file()]
        return "\n".join(f"- {i}" for i in sorted(items)[:30]) if items else "（空）"

    # ---------- 项目内容（整个群聊共享，和成员记忆是两回事） ----------
    def _project_path(self) -> Path:
        return self.project_dir / "project.md"

    def _read_project(self) -> str:
        p = self._project_path()
        if not p.exists():
            return "（这个群聊还没有项目笔记。可以在界面里补上项目背景、目标、约束）"
        return p.read_text(encoding="utf-8", errors="ignore")[-3500:]

    def _append_project(self, heading: str, body: str) -> None:
        body = (body or "").strip()
        if not body:
            return
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        p = self._project_path()
        if not p.exists():
            p.write_text(f"# 项目笔记 · {self.room_id}\n", encoding="utf-8")
        stamp = time.strftime("%m-%d %H:%M")
        with p.open("a", encoding="utf-8") as f:
            f.write(f"\n## [{stamp}] {heading}\n\n{body[:1200]}\n")

    def _prompt(self, member: Adapter, trigger: str) -> str:
        roster = "\n".join(
            f"- {m.name}（{m.role}{'，' + m.spec['persona'] if m.spec.get('persona') else ''}）"
            for m in self.members.values())
        return f"""你在一个多智能体协作房间里工作，和几个队友共同完成目标。
所有成员共享同一条对话流、同一个工作目录，以及同一份项目笔记。
但每个成员的长期记忆是私有的，你看不到队友的。

【项目笔记】（整个群聊共享，包含背景与历史进展）
{self._read_project()}

【房间最近对话】
{self.store.transcript(self.room_id, limit=16)}

【工作目录现有文件】
{self._files()}

【团队成员】
{roster}

【你的身份】
{member.system_prompt}

【你的长期记忆】
下面是你自己在以往任务里攒下的经验，不是别人告诉你的：
{self._read_memory(member)}

【现在轮到你】
{trigger}

【规则】
1. 只做你负责的那部分，不要替队友做他们的活。
2. 如果你需要某个队友接手，在回复的最后单独一行写：@队友名 具体要他做什么
3. 不需要别人接手就直接给结论，不要写 @。
4. 回复本身控制在 400 字以内；长内容写进工作目录的文件，回复里只说明文件路径和要点。
5. 保持你的性格，不要变成千篇一律的助手腔。"""
