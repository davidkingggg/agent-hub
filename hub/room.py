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
                 leader: str = "", leader_max_runs: int = 3,
                 max_calls: int = 40, on_event=None):
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
        # 真正的成本闸门是「总共调用了多少次模型」，不是「嵌套了几层」。
        # 层数只用来防深度爆炸，不该拿来当任务轮次上限——Leader 模式下
        # 一次「派发-验收」就要吃掉 2 层，6 层其实只够 3 轮。
        self.max_calls = max(1, max_calls)
        self._calls = 0
        self._wrapped = False
        # 负责人：它派出去的活，结果会汇聚回它这里验收。
        # 它需要多轮发言（派发 → 验收 → 返工），所以单独给一个更高的次数上限。
        self.leader = leader if leader in self.members else ""
        self.leader_max_runs = max(1, leader_max_runs)
        self.verbose = verbose
        self.on_event = on_event
        self._runs: dict[str, int] = {}

    def _limit(self, name: str) -> int:
        """这个成员本轮最多能跑几次。"""
        return self.leader_max_runs if name == self.leader else self.max_runs

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
        self._calls = 0                    # 预算按「每次用户请求」重新计
        self._wrapped = False
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

    # ---------- 预算与收尾 ----------

    def _guard(self, depth: int) -> bool:
        """还能不能继续跑。到限不是硬停，而是走收尾流程。"""
        if self._calls >= self.max_calls:
            self._wrap_up(f"调用次数用满 {self.max_calls} 次")
            return False
        if depth > self.max_hops:
            self._wrap_up(f"嵌套到 {self.max_hops} 层")
            return False
        return True

    def _wrap_up(self, reason: str) -> None:
        """不再派新活，但给负责人一次收尾机会——戛然而止最让人困惑。

        硬停的表现是「界面突然没动静了」，用户不知道是成功、失败还是卡住。
        这里让负责人交代清楚：做完了什么、差什么、下一步建议。
        """
        if self._wrapped:
            return
        self._wrapped = True

        self._log(f"  ⏹ {reason}，进入收尾")
        self._emit("status", text=f"{reason}，让负责人收尾")

        if not self.leader or self.leader not in self.members:
            self._emit("status", text=f"已停止：{reason}")
            return

        self._runs[self.leader] = self._runs.get(self.leader, 0) + 1
        try:
            out = self._run_one(self.members[self.leader], (
                f"（预算用尽：{reason}）\n"
                "不要再派发任何新任务，也不要 @ 任何人。\n"
                "基于目前已经拿到的结果，给用户一个明确的收尾说明：\n"
                "1. 已经完成了什么（有产出就指出文件路径）\n"
                "2. 还差什么、为什么没做完\n"
                "3. 建议用户下一步怎么做（比如把剩下的部分单独拆成一次任务）"
            ))
        except Exception as e:
            self._emit("error", text=f"收尾失败：{type(e).__name__}: {e}")
            return

        if out and not out.startswith("["):
            self.store.add(self.room_id, self.leader, out, kind="result")
            self._emit("message", sender=self.leader, content=out, kind="result")
            self._append_project(self.leader, out)
            self._log(f"\n{self.leader}（收尾）: {out[:200]}")
        self._emit("status", text="已收尾")

    # ---------- 内部 ----------
    def _dispatch(self, trigger: str, depth: int, parallel: bool = True,
                  force_targets: list[str] | None = None) -> None:
        if not self._guard(depth):
            return

        targets = force_targets or self._targets(trigger)
        if not targets:
            if not self.default:
                return
            targets = [self.default]

        # 同一轮里同一个成员最多跑 N 次（负责人另算），避免重复劳动
        skipped = [t for t in targets if self._runs.get(t, 0) >= self._limit(t)]
        if skipped:
            self._log(f"  （{', '.join(skipped)} 本轮已执行过，跳过）")
        targets = [t for t in targets if self._runs.get(t, 0) < self._limit(t)]
        if not targets:
            return
        for t in targets:
            self._runs[t] = self._runs.get(t, 0) + 1
        self._calls += len(targets)

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

        for name in targets:                       # 处理交接 / 派发
            out = results.get(name) or ""
            nxt = [n for n in self._targets(out, strict=True) if n != name]
            if not nxt:
                continue

            if name == self.leader:
                # 负责人派活：并发执行，全部完成后合并成一次交回来验收。
                # 这是和「普通交接」的关键区别——结果不往下传，而是回到派发者。
                self._fan_out(nxt, out, leader=name, depth=depth + 1)
            else:
                # 普通成员交接：结果交给下游，下游接着做
                for target in nxt:
                    self._log(f"  ↪ {name} 交接给 {target}")
                    self._dispatch(f"（来自 {name} 的交接）\n{out}", depth + 1,
                                   parallel=False, force_targets=[target])

    def _fan_out(self, targets: list[str], trigger: str,
                 leader: str, depth: int) -> None:
        """负责人派发：并发执行，等全部完成，合并结果一次性交回负责人。

        这是 Leader 模式的核心——没有这一步，负责人会收到 N 次孤立的
        「某某的交接」，每次只看到一部分结果，验收就无从谈起。
        """
        if not self._guard(depth):
            return

        targets = [t for t in targets if t in self.members]
        # 派发是负责人的明确指令，不是随机交接，所以放宽到 leader_max_runs：
        # 否则「验收不通过 → 打回重做」这一步会被 max_runs=1 直接拦掉。
        targets = [t for t in targets if self._runs.get(t, 0) < self.leader_max_runs]
        if not targets:
            self._log("  （派发目标本轮都已执行过，跳过）")
            return
        for t in targets:
            self._runs[t] = self._runs.get(t, 0) + 1
        self._calls += len(targets)

        self._log(f"  ⇉ 派发 {len(targets)} 个任务：{', '.join(targets)}")
        self._emit("status", text=f"派发 {len(targets)} 个任务：{', '.join(targets)}")

        results: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=min(4, len(targets))) as pool:
            futures = {pool.submit(self._run_one, self.members[t], trigger): t
                       for t in targets}
            for fut in as_completed(futures):
                name = futures[fut]
                try:
                    results[name] = fut.result()
                except Exception as e:
                    results[name] = f"[执行异常] {type(e).__name__}: {e}"
                # 逐个落库，用户能实时看到谁先做完
                out = (results[name] or "").strip()
                if out:
                    self.store.add(self.room_id, name, out, kind="result")
                    self._emit("message", sender=name, content=out, kind="result")
                    self._append_project(name, out)

        merged = "\n\n".join(
            f"【{n} 的产出】\n{r}" for n, r in results.items() if (r or "").strip())
        if not merged:
            self._log("  （全员无产出，跳过验收）")
            return

        self._log(f"  ✓ 汇聚 {len(results)}/{len(targets)} → {leader} 验收")
        self._emit("status", text=f"汇聚完成，交给 {leader} 验收")
        self._dispatch(f"（团队完成，请你验收）\n\n{merged}", depth + 1,
                       parallel=False, force_targets=[leader])

    def _run_one(self, member: Adapter, trigger: str) -> str:
        ok, why = member.available()
        if not ok:
            return f"[{member.name} 当前不可用] {why}"

        # 一次调用可能要跑几分钟，期间前端必须知道「它还在干活」，
        # 否则用户看到的就是一片死寂，以为请求失败了。
        self._emit("working", name=member.name, on=True)
        started = time.time()
        try:
            out = member.run(self._prompt(member, trigger), self.workdir)
        finally:
            self._emit("working", name=member.name, on=False,
                       seconds=round(time.time() - started, 1))

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

    def _rules(self, member: Adapter) -> str:
        """负责人的规则和普通成员完全不同，这里分开写。"""
        if member.name == self.leader:
            others = "、".join(n for n in self.members if n != self.leader) or "（没有其他成员）"
            return f"""【你的角色：负责人】
这个房间由你负责。用户只给目标，怎么落地由你决定，队友是 {others}。

【工作流程】
1. 拆解：把目标拆成具体任务，每项写清「谁做、做什么、什么算完成」。
2. 派发：在回复最后几行写 @队友 任务描述，可以写多个：
       @builder 实现 X，跑通后说明改了哪些文件
       @docs 写一份 X 的说明放到 docs/x.md
   写完这几行就停下，不要自己动手做他们的活。他们会并发执行，
   全部完成后结果会自动汇总回你这里，不用你催。
3. 验收：收到「团队完成，请你验收」后，逐项对照验收标准检查，
   明确说「通过」或「不通过，因为……」。能看文件就直接看文件，别只信自述。
4. 返工：不通过的就写 @队友 具体要改什么，会再跑一轮。
5. 收尾：全部通过后给用户一段总结，不要再 @ 任何人。

【纪律】
- 派发前先想清楚「什么算完成」，验收就按这个标准判，不要临时加码。
- 不要替队友做他们该做的事；你自己只做统筹和判断。
- 最多 3 轮。第 3 轮必须给结论，哪怕还不完美也要说清楚哪里没达到。"""

        return """【规则】
1. 只做你负责的那部分，不要替队友做他们的活。
2. 如果你需要某个队友接手，在回复的最后单独一行写：@队友名 具体要他做什么
3. 不需要别人接手就直接给结论，不要写 @。
4. 回复本身控制在 400 字以内；长内容写进工作目录的文件，回复里只说明文件路径和要点。
5. 保持你的性格，不要变成千篇一律的助手腔。"""

    def _prompt(self, member: Adapter, trigger: str) -> str:
        roster = "\n".join(
            f"- {m.name}（{m.role}{'，' + m.spec['persona'] if m.spec.get('persona') else ''}）"
            + ("  ← 负责人" if m.name == self.leader else "")
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

{self._rules(member)}"""
