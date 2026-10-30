"""远程控制端口的守护进程：拉起 remote.py + cloudflared，并盯着它们别死。

    python launch.py               前台守护（Ctrl+C 退出，会把子进程一起收掉）
    python launch.py --no-tunnel   只开局域网
    python launch.py --stop        停掉已在跑的实例

两个子进程都挂在它下面，谁退出就重启谁；隧道重启会换 URL，
会重新写 remote.url.txt 并在日志里打出来。

为什么不做成「启动完就退出的后台进程」：这台机器的安全策略禁用了
schtasks 和 WMI 建进程，Python 用 DETACHED_PROCESS 拉起来的子进程
也会随父进程所在的作业对象被回收。所以只能留一个守护进程常驻，
由调用方（WorkBuddy 的后台任务 / start.bat）负责让它活着。
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
PY = sys.executable
CF = ROOT / "bin" / "cloudflared.exe"
PID_FILE = ROOT / ".launch.pid"
TUNNEL_LOG = ROOT / "tunnel.log"
URL_FILE = ROOT / "remote.url.txt"
SRV_LOG = ROOT / "remote.out.log"
NO_WINDOW = 0x08000000


def clean_env() -> dict:
    """摘掉从 WorkBuddy 继承的 CODEBUDDY_*，否则子进程里的 CLI 会假「未登录」。"""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("CODEBUDDY_")}
    env.pop("PYTHONPATH", None)
    return env


def health(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def port_owner(port: int) -> list[int]:
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace").stdout
    except Exception:
        return []
    return [int(l.split()[-1]) for l in out.splitlines()
            if f":{port} " in l and "LISTENING" in l]


def lan_ip() -> str:
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def stop_all(port: int) -> None:
    for pid in port_owner(port):
        subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                       capture_output=True, creationflags=NO_WINDOW)
        print(f"  停止占用 {port} 的进程 {pid}")
    subprocess.run(["taskkill", "/IM", "cloudflared.exe", "/F"],
                   capture_output=True, creationflags=NO_WINDOW)
    PID_FILE.unlink(missing_ok=True)


class Sup:
    def __init__(self, port: int, tunnel: bool):
        self.port = port
        self.tunnel = tunnel
        self.srv: subprocess.Popen | None = None
        self.tnl: subprocess.Popen | None = None
        self.url = ""

    def start_srv(self) -> None:
        log = SRV_LOG.open("ab")
        self.srv = subprocess.Popen(
            [PY, str(ROOT / "remote.py"), "--port", str(self.port)],
            cwd=str(ROOT), env=clean_env(), stdin=subprocess.DEVNULL,
            stdout=log, stderr=log, creationflags=NO_WINDOW)
        log.close()
        for _ in range(40):
            time.sleep(0.4)
            if health(self.port):
                return
        print("服务没起来，看 remote.out.log", flush=True)

    def start_tnl(self) -> None:
        if not CF.exists():
            print("没装 bin/cloudflared.exe，跳过公网", flush=True)
            self.tunnel = False
            return
        TUNNEL_LOG.unlink(missing_ok=True)
        self.tnl = subprocess.Popen(
            [str(CF), "tunnel", "--url", f"http://127.0.0.1:{self.port}", "--no-autoupdate"],
            cwd=str(ROOT), env=clean_env(), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=TUNNEL_LOG.open("ab"),
            creationflags=NO_WINDOW)
        pat = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
        for _ in range(60):
            time.sleep(0.5)
            if TUNNEL_LOG.exists():
                m = pat.search(TUNNEL_LOG.read_text(encoding="utf-8", errors="replace"))
                if m:
                    self.url = m.group(0)
                    return
        print("隧道没连上，看 tunnel.log", flush=True)

    def report(self) -> None:
        token = (ROOT / ".remote_token").read_text(encoding="utf-8").strip()
        print("=" * 64, flush=True)
        print("  Agent Hub 远程控制已就绪", flush=True)
        print(f"  局域网  http://{lan_ip()}:{self.port}/?t={token}", flush=True)
        if self.url:
            print(f"  公网    {self.url}/?t={token}", flush=True)
            URL_FILE.write_text(f"{self.url}/?t={token}\n", encoding="utf-8")
            self.qr(f"{self.url}/?t={token}")
        print(f"  令牌    {token}", flush=True)
        print("=" * 64, flush=True)

    @staticmethod
    def qr(url: str) -> None:
        """顺手生成一张二维码，手机扫码直接进，省得在地址栏敲令牌。"""
        try:
            import segno
            segno.make(url, error="m").save(ROOT / "remote-qr.png", scale=6, border=2)
            print("  二维码  remote-qr.png", flush=True)
        except Exception:
            pass

    def run(self) -> None:
        for pid in port_owner(self.port):
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, creationflags=NO_WINDOW)
        PID_FILE.write_text(str(os.getpid()), encoding="ascii")
        self.start_srv()
        if self.tunnel:
            self.start_tnl()
        self.report()
        while True:
            time.sleep(5)
            if self.srv and self.srv.poll() is not None:
                print(f"[{time.strftime('%H:%M:%S')}] 服务退出，重启", flush=True)
                self.start_srv()
            if self.tunnel and self.tnl and self.tnl.poll() is not None:
                print(f"[{time.strftime('%H:%M:%S')}] 隧道断开，重连", flush=True)
                self.start_tnl()
                self.report()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--no-tunnel", action="store_true")
    ap.add_argument("--stop", action="store_true")
    args = ap.parse_args()
    for s in (sys.stdout,):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    if args.stop:
        print("停止：")
        stop_all(args.port)
        return

    try:
        Sup(args.port, not args.no_tunnel).run()
    except KeyboardInterrupt:
        print("\n退出，收掉子进程")
        for p in ("cloudflared.exe",):
            subprocess.run(["taskkill", "/IM", p, "/F"],
                           capture_output=True, creationflags=NO_WINDOW)
        PID_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
