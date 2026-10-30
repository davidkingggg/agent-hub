#!/data/data/com.termux/files/usr/bin/bash
# Termux 启动脚本（对应 Windows 的 start.bat），带首跑自举：
#   1. 没有 config.json → 从 config.example.termux.json 生成，
#      并把 HERMES_HOME 改写成本仓库的实际路径（换目录部署不用手改）
#   2. 没有 .hermes-home/ → 创建隔离家目录（主 config.yaml + .env 副本、
#      空 memories/ 和 skills/，保证成员不带宿主记忆）
#   3. 起服务
# 用法: ./start.sh        起服务
#       HUB_PORT=8899 ./start.sh   换端口
set -e
cd "$(dirname "$0")"
ROOT="$PWD"

if [ ! -f config.json ]; then
  cp config.example.termux.json config.json
  python3 - "$ROOT" <<'PY'
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
p = root / "config.json"
cfg = json.loads(p.read_text(encoding="utf-8"))
home = str(root / ".hermes-home")
for m in cfg.get("members", []):
    m.setdefault("env", {})["HERMES_HOME"] = home
p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"已生成 config.json，HERMES_HOME -> {home}")
PY
fi

HH="$ROOT/.hermes-home"
if [ ! -f "$HH/config.yaml" ]; then
  mkdir -p "$HH/memories" "$HH/skills"
  if [ -f ~/.hermes/config.yaml ]; then
    cp ~/.hermes/config.yaml "$HH/config.yaml"
    [ -f ~/.hermes/.env ] && cp ~/.hermes/.env "$HH/.env"
    echo "已初始化 .hermes-home（主配置副本，记忆/技能留空）"
  else
    echo "警告：没找到 ~/.hermes/config.yaml，hermes 未安装？服务照常启动，成员会不可用"
  fi
fi

exec python3 server.py
