"""验证模型自动降级：第一次调用返回 429，第二次用备用模型成功。"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from hub.adapters import CliAdapter

FAKE = '''import sys, os
m = os.path.join(os.path.dirname(__file__), ".calls")
n = int(open(m).read()) if os.path.exists(m) else 0
open(m, "w").write(str(n + 1))
sys.stdin.read()
if n == 0:
    print("429 usage exceeds frequency limit, reset at 13:16")
else:
    print("收到（备用模型生效）")
'''

tmp = Path(tempfile.mkdtemp())
fake = tmp / "fake.py"
fake.write_text(FAKE, encoding="utf-8")

a = CliAdapter({
    "name": "probe",
    "cmd": f'python "{fake}" --model {{model}}',
    "model": "free-model",
    "fallback_models": ["paid-model"],
    "timeout": 30,
})

out = a.run("测试", tmp)
calls = (tmp / ".calls").read_text()

print("最终输出:", out)
print("实际调用次数:", calls)
print("结果:", "通过（429 后自动切换）" if calls == "2" and "备用模型生效" in out
      else "不符合预期")
