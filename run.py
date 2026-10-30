"""本地多智能体协作房间。

    python run.py --check          检查每个成员现在能不能用
    python run.py --demo           用模拟成员跑一遍，验证协作流程
    python run.py                  进入交互模式
    python run.py "把 xxx 做完"     发一条就退出
    python run.py --history        看房间历史
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(ROOT))

from hub.adapters import build          # noqa: E402
from hub.room import Room               # noqa: E402
from hub.store import Store             # noqa: E402
from hub import vault                   # noqa: E402


def apply_path_extra(cfg: dict) -> None:
    """把 config.json 里声明的额外目录塞进 PATH。

    子进程不继承 shell 里的临时 export，所以 npm 全局 bin 这类目录
    必须在这里显式注入，否则 CliAdapter 会报「找不到命令」。
    """
    extra = cfg.get("path_extra") or []
    if not extra:
        return
    dirs = [str((ROOT / p).resolve()) if p.startswith(".") else os.path.expandvars(p)
            for p in extra]
    os.environ["PATH"] = os.pathsep.join(dirs + [os.environ.get("PATH", "")])


def load_config(room_key: str = "") -> dict:
    """和 GUI 共用同一份 config.json。"""
    cfg_path = ROOT / "config.json"
    if not cfg_path.exists():
        sys.exit(f"找不到配置文件 {cfg_path}")
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))

    # 补端口：成员没写 port 就按 port_base 顺序分配
    used = {m["port"] for m in cfg["members"] if m.get("port")}
    nxt = int(cfg.get("port_base", 3977))
    for m in cfg["members"]:
        if not m.get("port"):
            while nxt in used:
                nxt += 1
            m["port"] = nxt
            used.add(nxt)

    # 默认操作第一个未归档的房间，不再写死 rooms[0]（可能是已归档或空房间）
    rooms = cfg.get("rooms") or []
    room = None
    if room_key:
        room = next((r for r in rooms
                     if r.get("id") == room_key or r.get("name") == room_key), None)
        if room is None:
            choices = "、".join(f"{r.get('id')}({r.get('name', '')})" for r in rooms)
            sys.exit(f"找不到房间「{room_key}」，可选：{choices}")
    if room is None:
        room = next((r for r in rooms if not r.get("archived")), rooms[0] if rooms else {})
    cfg["_room"] = room
    pool = {m["name"]: m for m in cfg["members"]}

    def _expand(m: dict) -> dict:
        cmd = str(m.get("cmd", ""))
        cmd = cmd.replace("{port}", str(m.get("port", 3977)))
        cmd = cmd.replace("{model}", str(m.get("model") or "deepseek-v4.1-flash"))
        # {effort} 漏了会原样把 "--effort {effort}" 传给 codebuddy，
        # 表现成启动失败或参数非法，而 GUI 入口是展开的——两边行为必须一致。
        cmd = cmd.replace("{effort}", str(m.get("effort") or "ultracode"))
        return dict(m, cmd=cmd)

    cfg["members"] = [_expand(pool[n]) for n in room.get("members", list(pool)) if n in pool]
    cfg["workdir"] = f"./workspaces/{room.get('workdir', room.get('id', 'main'))}"
    cfg["default_member"] = room.get("default", "")
    return cfg


def make_room(cfg: dict, demo: bool = False) -> Room:
    specs = cfg["members"]
    if demo:
        # 全部换成模拟成员，并串成一条交接链，用来验证 @ 转交是否生效
        names = [s["name"] for s in specs]
        specs = [{"name": s["name"], "role": s.get("role", ""), "type": "mock",
                  "system_prompt": s.get("system_prompt", ""),
                  "handoff_to": names[i + 1] if i + 1 < len(names) else ""}
                 for i, s in enumerate(cfg["members"])]
    members = [build(s) for s in specs]

    workdir = (ROOT / cfg.get("workdir", "./workspaces/main")).resolve()
    store = Store(ROOT / "hub.db")
    room = cfg.get("_room", {})
    room_id = store.ensure_room(room.get("id", "main"))
    leader = room.get("leader", "")
    default_hops = 12 if leader else 6
    default_calls = 40 if leader else 20
    return Room(store, room_id, members, workdir,
                default_member=cfg.get("default_member", ""),
                max_hops=int(room.get("max_hops", default_hops)),
                max_runs=int(room.get("max_runs", 1)),
                leader=leader,
                leader_max_runs=int(room.get("leader_max_runs", 3)),
                max_calls=int(room.get("max_calls", default_calls)),
                room_name=room.get("name") or room_id,
                project_name=room.get("project") or room.get("workdir") or room_id)


def cmd_check(cfg: dict) -> None:
    print("成员可用性检查\n" + "=" * 46)
    for spec in cfg["members"]:
        m = build(spec)
        ok, why = m.available()
        mark = "可用  " if ok else "不可用"
        print(f"{mark}  {m.name:<12} [{m.kind}] {m.role}")
        if not ok:
            print(f"        └─ {why}")
    print("=" * 46)


def main() -> None:
    for stream in (sys.stdout, sys.stdin):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="本地多智能体协作房间")
    ap.add_argument("message", nargs="*", help="直接发送一条消息")
    ap.add_argument("--room", default="", help="指定房间（id 或名字），默认第一个未归档的")
    ap.add_argument("--demo", action="store_true", help="全部成员用模拟实现")
    ap.add_argument("--check", action="store_true", help="只检查成员可用性")
    ap.add_argument("--history", action="store_true", help="打印房间历史")
    args = ap.parse_args()

    cfg = load_config(args.room)
    apply_path_extra(cfg)

    # 和 GUI 共用同一个 Obsidian 仓库，先把笔记和索引补齐
    try:
        raw = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        vault.sync_vault(ROOT / "projects", raw)
    except Exception as e:
        print(f"（记忆库同步跳过：{type(e).__name__}: {e}）")

    if args.check:
        cmd_check(cfg)
        return

    room = make_room(cfg, demo=args.demo)

    if args.history:
        room.replay()
        return

    if args.message:
        room.say(" ".join(args.message))
        return

    names = ", ".join(room.members)
    room_name = cfg.get("_room", {}).get("name") or cfg.get("_room", {}).get("id", "main")
    print(f"房间「{room_name}」已就绪，成员：{names}")
    print("用 @名字 指定谁来做；输入 /exit 退出，/history 看记录\n")
    while True:
        try:
            text = input("你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text in ("/exit", "/quit", "/q"):
            break
        if text == "/history":
            room.replay()
            continue
        room.say(text)
    print("已退出。")


if __name__ == "__main__":
    main()
