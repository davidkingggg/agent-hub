"""用脚本化的假成员验证 Leader 模式的派发-汇聚-验收-返工闭环。

不联网、不调真实 agent，只验证调度逻辑。
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from hub.adapters import Adapter
from hub.room import Room
from hub.store import Store


class Scripted(Adapter):
    """按预设脚本逐次回复，并记录被调用了几次。"""
    kind = "mock"

    def __init__(self, spec, script):
        super().__init__(spec)
        self.script = list(script)
        self.calls = 0

    def run(self, prompt, cwd):
        self.calls += 1
        reply = self.script.pop(0) if self.script else "（脚本用尽）"
        print(f"    [{self.name}] 第 {self.calls} 次被调用 → {reply[:40]}")
        return reply


lead = Scripted({"name": "lead", "role": "统筹", "system_prompt": "你是负责人"}, [
    "拆解如下：\n@a 做 X，做完说清楚改了哪些文件\n@b 做 Y，输出到 y.md",
    "验收：a 通过。b 不通过，缺边界处理。\n@b 补上空值和超长的处理",
    "全部通过。X 和 Y 都完成，遗留：无。",
])
a = Scripted({"name": "a", "role": "工程", "system_prompt": "你是工程师"},
             ["X 做完了，改了 calc.py"])
b = Scripted({"name": "b", "role": "文档", "system_prompt": "你是文档"},
             ["Y 写好了，在 y.md", "补好了空值和超长"])

tmp = Path(tempfile.mkdtemp())
store = Store(tmp / "test.db")
rid = store.ensure_room("test")

room = Room(store, rid, [lead, a, b], tmp / "ws" / "room1",
            leader="lead", leader_max_runs=3, max_hops=6, verbose=True)

print("=" * 60)
room.say("把 X 和 Y 都做出来")
print("=" * 60)

print(f"\n调用次数：lead={lead.calls}  a={a.calls}  b={b.calls}")
print("\n消息流：")
for m in store.history(rid, 30):
    print(f"  [{m['sender']}] {m['content'][:60].replace(chr(10), ' / ')}")

ok = lead.calls == 3 and a.calls == 1 and b.calls == 2
print(f"\n结果：{'通过' if ok else '不符合预期'}")

# ---------- 兼容性：不配 leader 时应该完全走原来的交接逻辑 ----------
print("\n" + "=" * 60)
print("兼容性测试：不配 leader，两个成员互相交接")
x = Scripted({"name": "x", "role": "研究", "system_prompt": ""},
             ["我查过了，结论在 r.md\n@y 你接着写成文档"])
y = Scripted({"name": "y", "role": "撰写", "system_prompt": ""}, ["文档写好了"])

store2 = Store(tmp / "t2.db")
rid2 = store2.ensure_room("t2")
room2 = Room(store2, rid2, [x, y], tmp / "ws" / "room2", leader="", verbose=True)
room2.say("@x 先查一下再出文档")

ok2 = x.calls == 1 and y.calls == 1
print(f"\n调用次数：x={x.calls}  y={y.calls}（期望各 1 次）")
print(f"结果：{'通过' if ok2 else '不符合预期'}")

