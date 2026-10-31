# Leader 模式设计：从 Peer-to-Peer 到 Orchestrator + Supervisor

> 目标：用户只下目标，由某个成员担任 Leader，负责拆解、派发、监督、验收、汇报。

---

## 一、现状诊断：为什么必须你亲自统筹

当前调度器是**平权点对点**（Peer-to-Peer / Swarm）：谁 `@` 谁，点对点交接。
四个机制共同导致 Leader 无法成立：

| 机制 | 位置 | 后果 |
|---|---|---|
| `max_runs=1` | `room.py:105` | 成员一轮只能发言一次，Leader 派完活就没机会验收 |
| `max_hops=3` | `room.py:90` | Leader→A→B 就顶格，验收排不进去 |
| 链式交接不汇聚 | `room.py:143-148` | 每个交接独立触发，Leader 收到 N 次碎片化输入 |
| 无任务状态 | 全局 | 系统只知道消息，不知道「谁负责什么、做到哪」 |

### 关键代码

```python
# room.py:143-148  ——  交接是「每个下游单独开一次」
for name in targets:
    out = results.get(name) or ""
    nxt = [n for n in self._targets(out, strict=True) if n != name]
    for target in nxt:
        self._dispatch(f"（来自 {name} 的交接）\n{out}", depth + 1, parallel=False)
```

Leader 同时派给 A、B 时：A 完成 → 触发一次 Leader；B 完成 → 再触发一次。
两次都是孤立触发，各带一半信息，且第二次被 `max_runs=1` 直接跳过。

**结论：不是 prompt 写得不好，是拓扑不支持。**

---

## 二、业界怎么做

### 2.1 四种拓扑对比

| 维度 | Orchestrator/Worker | Supervisor | **Peer-to-Peer** | Pipeline |
|---|---|---|---|---|
| 控制方式 | 中心化 | 中心化审查 | 去中心化 | 顺序传递 |
| 复杂度 | 低 | 中 | 高 | 中 |
| 可调试性 | 高 | 高 | **低** | 中 |
| 一致性 | 强 | 强 | **弱** | 中 |
| 并行度 | Worker 可并行 | 串行（审核等待） | 天然并行 | 阶段间可并行 |
| 典型场景 | 研究团队、任务分发 | 代码审查、内容审核 | 开放 Agent 网络 | 数据处理 |

**当前项目 = Peer-to-Peer**。这张表直接解释了现象：去中心化程度最高，但调试最难、一致性最弱。

### 2.2 Claude Code 的 subagent 机制

这是最贴近我们场景的参考实现（`learn-claude-code` s04/s06）。

```python
# 父端：task 是一个普通工具
if block.name == "task":
    output = run_subagent(prompt)      # 同步阻塞，等子循环跑完
    results.append({"type": "tool_result", "content": output})

# 子端：全新 messages[]，独立循环
def run_subagent(prompt: str) -> str:
    sub_messages = [{"role": "user", "content": prompt}]   # 全新上下文
    for _ in range(30):                                     # 安全上限
        response = client.messages.create(system=SUBAGENT_SYSTEM, messages=sub_messages)
        sub_messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            break
        # 执行工具，结果 append 回 sub_messages
    return "".join(b.text for b in response.content if hasattr(b, "text"))
```

四个关键设计：

| 设计 | 作用 |
|---|---|
| **同步阻塞** | 父循环等子循环结束才继续 → 天然汇聚，不需要额外协调 |
| **只返回最终文本** | 子循环几十轮工具调用记录直接丢弃，不污染父上下文 |
| **共享文件系统** | 子 agent 写的文件父 agent 能直接读，验收有据可查 |
| **子端工具池不含 task** | 硬性禁止递归委派，防止无限嵌套 |

最后一条尤其重要：**单层委派**是可控性的前提。

### 2.3 Supervisor 的迭代上限

业界共识：**验收循环最多 3 次**。超过说明 Worker 或 Supervisor 本身设计有问题，继续循环只会烧钱。

```python
for i in range(3):
    content = await writer.write(topic)
    feedback = await supervisor.review(content)
    if feedback["passed"]:
        return content
    topic = f"{topic}，改进：{feedback['suggestions']}"
raise MaxRetriesExceeded()
```

> **本项目的取舍（2026-10-30 更新）**：上面的 3 次是业界默认值，本项目最终把上限放到 **50 步**。
>
> 3 次的前提是「单次交付 + 一次返工」。但真实任务是**多步推进 + 持续优化**：
> 查数据 → 测算 → 改代码 → 跑测试 → 回放 → 对比 → 写文档，七步起步，
> 每步都可能打回。用 3 次去卡一条七步的链路，等于第一步就没余量。
>
> 所以这里把「轮次」重新定义成「步数」：**一步 = 负责人发言一次并派发一轮**。
> 50 步覆盖绝大多数任务，且允许返工穿插其间。
>
> 放宽后，防打转不再靠轮次，改成靠两条：
> 1. **打回必须说清改哪一处、改成什么样**（写进负责人提示词，禁止"再优化一下"）
> 2. **`max_calls` 兜底**（400 次调用），钱花完就强制收尾
>
> 步数是"允许推进多远"，预算是"最多花多少钱"，两者不能互相替代。

---

## 三、方案设计

### 3.1 目标拓扑

```
用户
 │  只给目标
 ▼
Leader ──── 拆解 ────┐
 │                    │
 ├── @A 任务1 ──┐     │  并发派发
 ├── @B 任务2 ──┼─────┘
 └── @C 任务3 ──┘
                  │
        ┌─────────┴─────────┐
        │  全部完成 → 汇聚    │   ← 这是现在缺的那一环
        └─────────┬─────────┘
                  ▼
              Leader 验收
                  │
        ┌─────────┴─────────┐
        │ 合格 → 汇报用户     │
        │ 不合格 → @A 返工    │  ← 有限轮（3 次）
        └───────────────────┘
```

### 3.2 三个核心机制

#### 机制一：派发语义 —— 谁派的活，结果回谁那

不引入新语法。复用现有的「行首 @」约定，但改变结果去向：

- **Leader 发言**最后几行有多个 `@成员` → 判定为**派发**
- 派发的任务**并发执行**，结果**不往下传，回到 Leader**

其他成员的 `@` 保持原语义（交接给下游继续做），不破坏现有用法。

#### 机制二：汇聚 —— 兄弟任务全部完成才触发

引入批次（batch）概念：

```python
batch = {
    "id": "b1",
    "leader": "pm",
    "targets": ["a", "b", "c"],
    "results": {"a": "...", "b": "..."},   # c 还没回来
    "depth": 2,
}
# 当 len(results) == len(targets) 时，合并成一次调用交给 leader
```

合并后的触发内容：

```
（团队完成，请你验收）
【a 的产出】
...
【b 的产出】
...
【c 的产出】
...
```

Leader 一次看到全部结果，而不是被拆成三次孤立输入。

#### 机制三：有限轮 —— 用 max_hops 兜底

- `max_hops` 从 3 提到 **108**（一次派发-验收吃 2 层，够 50 步）
- `leader_max_runs` 从 1 提到 **50**（负责人可多次发言；成员在一次派发里也能被执行 50 次）
- `max_calls` 提到 **400**（真正的成本闸门，50 步满员约 400 次）
- Leader 的 `max_runs` 单独放开（默认 20），其他成员保持 1
- 到达上限时明确告知 Leader「轮次用尽，直接给结论」

### 3.3 配置

```json
{
  "id": "r1234567",
  "name": "项目组",
  "leader": "pm",
  "max_hops": 108,
  "max_runs": 1,
  "max_calls": 400,
  "leader_max_runs": 50,
  "members": ["pm", "researcher", "builder", "reviewer"]
}
```

`leader` 为空时完全退回现有行为——**向后兼容**。

### 3.4 代码改动

改动集中在 `hub/room.py`，不动 adapter 和存储。

**① `_dispatch` 增加 `batch` 参数**

```python
def _dispatch(self, trigger: str, depth: int, parallel: bool = True,
              batch: dict | None = None) -> None:
```

**② `max_runs` 判定区分 Leader**

```python
limit = self.leader_max_runs if t == self.leader else self.max_runs
targets = [t for t in targets if self._runs.get(t, 0) < limit]
```

**③ 交接段改为「派发 or 交接」二选一**

```python
for name in targets:
    out = results.get(name) or ""
    nxt = [n for n in self._targets(out, strict=True) if n != name]
    if not nxt:
        continue

    if name == self.leader and len(nxt) > 1:
        # 派发：并发执行，全部完成后汇聚回 leader
        self._fan_out(nxt, out, leader=name, depth=depth + 1)
    else:
        # 传统交接：结果给下游，下游继续做
        for target in nxt:
            self._dispatch(f"（来自 {name} 的交接）\n{out}",
                           depth + 1, parallel=False)
```

**④ 新增 `_fan_out`**

```python
def _fan_out(self, targets: list[str], trigger: str,
             leader: str, depth: int) -> None:
    if depth > self.max_hops:
        return

    results: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=min(4, len(targets))) as pool:
        futures = {pool.submit(self._run_one, self.members[t], trigger): t
                   for t in targets if t in self.members}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                results[name] = fut.result()
            except Exception as e:
                results[name] = f"[执行异常] {type(e).__name__}: {e}"
            self.store.add(self.room_id, name, results[name], kind="result")
            self._emit("message", sender=name, content=results[name], kind="result")

    # 全部到齐，合并成一次交给 leader
    merged = "\n\n".join(f"【{n} 的产出】\n{r}" for n, r in results.items())
    self._dispatch(f"（团队完成，请你验收）\n{merged}", depth + 1, parallel=False)
```

**⑤ 新增 `leader` / `leader_max_runs` 字段**

`__init__` 里接收，`server.py` 的 `build_room` 从房间配置读。

### 3.5 Leader 的 Prompt

```text
【你的角色】
你是这个项目的负责人。用户只给目标，具体怎么落地由你决定。

【工作流程】
1. 拆解：把目标拆成具体任务，每个任务明确「谁做、做什么、什么算完成」
2. 派发：在回复最后几行写 @成员 任务描述，可以写多个，会并发执行
   例：
   @researcher 查清 A 的现状，输出一页结论
   @builder 按 A 的方案实现，跑通后报告改了什么
   （写完这些你就停下，等他们做完，结果会自动汇总回你这里）
3. 验收：收到汇总后逐项检查，明确说「通过」或「不合格，因为…」
4. 返工：不合格的写 @成员 具体要改什么
5. 收尾：全部通过后，给用户一段总结，不要再 @ 任何人

【纪律】
- 不要自己动手做成员该做的事
- 每次派发前先想清楚「什么算完成」，验收时按这个标准判
- 最多 50 步（每步 = 发言一次并派发一轮），可以边做边优化
- 打回必须说清改哪一处、改成什么样；同一个改法两次无效就换思路
```

---

## 四、取舍

| 得到 | 失去 |
|---|---|
| 用户只下目标，Leader 闭环 | Leader 成为单点，它跑挂了整轮就卡住 |
| 结果汇聚，验收有完整依据 | 派发是同步等待，总耗时 = 最慢那个成员 |
| 拓扑清晰，日志可追溯 | 多一层 LLM 调用，成本上升 |
| 向后兼容（不配 leader 就是原行为） | 需要 Leader 的 prompt 写得好，否则拆解质量差 |

### 为什么不直接做任务板（结构化 JSON）

CLI agent 不保证每次吐出合法 JSON，要做校验 + 重试 + 降级，复杂度翻倍。
先用「自然语言 + 约定语法」跑通闭环，等确认这套流程真的有用，再考虑结构化。

### 为什么不支持多层委派

`learn-claude-code` 明确禁止子 agent 再调 `task`（子端工具池里没有这个工具）。
**单层委派是可控性的前提**——允许递归就会出现无限嵌套，成本和调试难度指数上升。

---

## 五、实施步骤

1. **改 room.py**：`_dispatch` 加 batch、`_fan_out`、leader 的 max_runs 区分
2. **改 server.py / run.py**：`build_room` 读取 `leader`、`leader_max_runs`
3. **改前端**：成员卡片上标出「Leader」徽标；房间设置里可指定 Leader
4. **写 Leader prompt**：加进 templates/ 作为「项目负责人」模板
5. **验证**：3 个成员的房间，Leader 派 2 个任务，看是否并发 + 汇聚 + 验收

### 验证用例

```
用户：把 calc.py 补上单元测试，并写一份说明

期望轨迹：
pm: 拆解 → @builder 写测试 @docs 写说明
    （并发 2 个任务）
builder: 测试写好了，覆盖 12 个用例
docs: 说明写好了，8 节
    （两个都完成后，合并成一次交给 pm）
pm: 验收 → builder 的测试通过了；docs 缺边界情况，@docs 补上
docs: 补好了
    （再次汇聚）
pm: 全部通过，总结给用户
```

日志应该能看到：`→ 派发 2 个任务` / `⏳ 等待 b, d` / `✓ 汇聚 2/2 → pm`。

---

## 六、参考

- [多 Agent 架构模式：Orchestrator/Supervisor/Peer/Pipeline 怎么选](https://www.51cto.com/article/859216.html)
- [learn-claude-code · Subagent 实现](https://github.com/shareAI-lab/learn-claude-code)
- [agency-agents](https://github.com/msitarzewski/agency-agents)
