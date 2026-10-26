"""成员适配器：把不同形态的 Agent 统一成 run(prompt, cwd) -> str。

三种类型：
  cli  —— 调用任意命令行 Agent（codex exec / claude -p / gemini -p ...）
  api  —— OpenAI 兼容端点 + 内置工具循环（DeepSeek / 通义 / Ollama ...）
  mock —— 离线演示，不联网

新增一个成员通常只需要改 members.json，不用动代码。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path


class Adapter:
    kind = "base"

    def __init__(self, spec: dict):
        self.spec = spec
        self.name = spec["name"]
        self.role = spec.get("role", "成员")
        self.system_prompt = spec.get("system_prompt", "")
        self.timeout = int(spec.get("timeout", 300))

    def available(self) -> tuple[bool, str]:
        return True, ""

    def run(self, prompt: str, cwd: Path) -> str:
        raise NotImplementedError


# ============================ CLI 型 ============================

class CliAdapter(Adapter):
    """把 prompt 从 stdin 喂进去，收 stdout。

    用 stdin 而不是拼命令行参数，是为了绕开引号和换行转义的地狱。

    注意 env 处理：如果这个进程是从 WorkBuddy 桌面版拉起来的，会继承一堆
    CODEBUDDY_* 变量（比如 CODEBUDDY_API_KEY_HELPER_DISABLED=1），
    它们会让 codebuddy CLI 自己的凭据查找失效，表现成「未登录」。
    所以调用前要把这类变量摘掉。
    """

    kind = "cli"

    # 只放行这些基础变量。从桌面版继承来的 CODEBUDDY_* / 代理变量会让
    # CLI 自己的认证和网络配置失效，实测必须摘干净才能正常调用。
    _KEEP = {"PATH", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "HOME",
             "SYSTEMROOT", "COMSPEC", "TEMP", "TMP", "USERNAME", "LANG"}
    _KEEP_PREFIX = ("ANTHROPIC_", "OPENAI_", "DEEPSEEK_", "GEMINI_",
                    "GOOGLE_", "TAVILY_", "QWEN_", "MOONSHOT_")

    def _env(self) -> dict:
        env = {k: v for k, v in os.environ.items() if k in self._KEEP}
        for k, v in os.environ.items():
            if k.startswith(self._KEEP_PREFIX):
                env[k] = v
        extra = self.spec.get("env") or {}
        env.update({k: str(v) for k, v in extra.items()})
        return env

    def available(self) -> tuple[bool, str]:
        cmd = (self.spec.get("cmd") or "").strip()
        if not cmd:
            return False, "没有配置 cmd"
        exe = cmd.split()[0]
        if not shutil.which(exe):
            return False, f"PATH 里找不到 `{exe}`"
        return True, ""

    def run(self, prompt: str, cwd: Path) -> str:
        cmd = self.spec["cmd"]
        try:
            p = subprocess.run(
                cmd, shell=True, input=prompt, cwd=str(cwd),
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=self.timeout, env=self._env())
        except subprocess.TimeoutExpired:
            return f"[超时：超过 {self.timeout}s，已终止]"
        except Exception as e:
            return f"[启动失败] {type(e).__name__}: {e}"

        out = (p.stdout or "").strip()
        err = (p.stderr or "").strip()
        if p.returncode != 0 and not out:
            return f"[退出码 {p.returncode}]\n{err[-900:]}"
        return out or err[-900:] or "[没有输出]"


# ============================ API 型 ============================

_TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "list_files", "description": "列出工作目录下的文件",
        "parameters": {"type": "object", "properties": {
            "subdir": {"type": "string"}}, "required": []}}},
    {"type": "function", "function": {
        "name": "read_file", "description": "读取工作目录里的文件",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file", "description": "把内容写入工作目录，队友可见",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"]}}},
]


def _safe_path(root: Path, rel: str) -> Path:
    p = (root / rel).resolve()
    if not str(p).startswith(str(root.resolve())):
        raise PermissionError(f"越界：{rel}")
    return p


def _exec_tool(name: str, args: dict, cwd: Path) -> str:
    try:
        if name == "list_files":
            base = _safe_path(cwd, args.get("subdir", "")) if args.get("subdir") else cwd
            files = [str(f.relative_to(cwd)) for f in sorted(base.rglob("*")) if f.is_file()]
            return "\n".join(files) if files else "（空目录）"
        if name == "read_file":
            p = _safe_path(cwd, args["path"])
            return p.read_text(encoding="utf-8", errors="ignore")[:6000] if p.is_file() else "文件不存在"
        if name == "write_file":
            p = _safe_path(cwd, args["path"])
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(args["content"], encoding="utf-8")
            return f"已写入 {args['path']}"
        return f"未知工具 {name}"
    except Exception as e:
        return f"工具失败：{type(e).__name__}: {e}"


class ApiAdapter(Adapter):
    kind = "api"

    def __init__(self, spec: dict):
        super().__init__(spec)
        self.base_url = (spec.get("base_url") or "").rstrip("/")
        self.model = spec.get("model", "")
        self.key_env = spec.get("api_key_env", "")
        self.max_steps = int(spec.get("max_steps", 6))
        self.use_tools = bool(spec.get("tools", True))

    def available(self) -> tuple[bool, str]:
        if not self.base_url or not self.model:
            return False, "缺少 base_url / model"
        if self.key_env and not os.environ.get(self.key_env):
            return False, f"环境变量 {self.key_env} 未设置"
        return True, ""

    def _chat(self, messages: list[dict], key: str, schemas) -> dict:
        body = {"model": self.model, "messages": messages, "temperature": 0.3}
        if schemas:
            body["tools"] = schemas
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {key}"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))["choices"][0]["message"]
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:300]
            raise RuntimeError(f"HTTP {e.code}: {detail}") from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"连不上 {self.base_url}: {e.reason}") from None

    def run(self, prompt: str, cwd: Path) -> str:
        key = os.environ.get(self.key_env, "none") if self.key_env else "none"
        messages = [
            {"role": "system", "content": self.system_prompt or f"你是团队里的{self.role}，名字叫 {self.name}。"},
            {"role": "user", "content": prompt},
        ]
        schemas = _TOOL_SCHEMAS if self.use_tools else None
        try:
            for _ in range(self.max_steps):
                msg = self._chat(messages, key, schemas)
                calls = msg.get("tool_calls") or []
                if not calls:
                    return (msg.get("content") or "").strip() or "[空回复]"
                messages.append({"role": "assistant",
                                 "content": msg.get("content") or "",
                                 "tool_calls": calls})
                for tc in calls:
                    fn = tc["function"]
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    messages.append({"role": "tool", "tool_call_id": tc["id"],
                                     "content": _exec_tool(fn["name"], args, cwd)})
        except Exception as e:
            return f"[调用失败] {e}"
        return "[达到工具步数上限，未收敛]"


# ============================ 演示型 ============================

class MockAdapter(Adapter):
    """不联网，用固定话术验证编排逻辑。

    会真的往工作目录写一个文件，用来证明「共享工作区」这条链路是通的。
    """

    kind = "mock"

    def run(self, prompt: str, cwd: Path) -> str:
        marker = "【现在轮到你】"
        body = prompt.split(marker)[-1].strip() if marker in prompt else prompt.strip()
        first = body.splitlines()[0][:70] if body.strip() else "（空请求）"

        f = cwd / f"{self.name}_产出.md"
        f.write_text(f"# {self.name} 的产出\n\n收到：{first}\n", encoding="utf-8")

        lines = [
            f"（模拟）我是 {self.name}，{self.role}。",
            f"处理内容：{first}",
            f"产出已写入 {f.name}。",
        ]
        nxt = self.spec.get("handoff_to")
        if nxt:
            lines.append(f"@{nxt} 这部分请你接手")
        return "\n".join(lines)


def build(spec: dict) -> Adapter:
    kind = spec.get("type", "mock")
    cls = {"cli": CliAdapter, "api": ApiAdapter, "mock": MockAdapter}.get(kind)
    if cls is None:
        raise ValueError(f"未知成员类型：{kind}")
    return cls(spec)
