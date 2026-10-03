from __future__ import annotations
import argparse
import json
import os
import urllib.request
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from .config import Config
from .scheduler import Scheduler
from .state import State
from .web import make_server

LABEL = "com.codex-autoresume"


def _t(ts):
    return time.strftime("%a %H:%M", time.localtime(ts)) if ts else "-"


def format_status(snap: dict) -> str:
    lines = [f"engine: {snap['engine']}  {snap.get('note', '')}".rstrip()]
    lim = snap.get("limits") or {}
    for name, key in (("5h window", "primary"), ("weekly", "secondary")):
        w = lim.get(key)
        lines.append(f"{name} {w['used_percent']}%  resets {_t(w['resets_at'])}" if w else f"{name} unknown")
    lines.append(f"usage allowed: {lim.get('allowed')}")
    lines.append("")
    if not snap["entries"]:
        lines.append("no threads blocked on the usage limit")
    for e in snap["entries"]:
        st = e["state"] + (f" #{e['position']}" if e.get("position") else "")
        when = _t(e["resume_at"]) if e["state"] == "scheduled" and e["position"] == 1 else ("after previous" if e["state"] == "scheduled" else "")
        lines.append(f"{e['thread_id'][:13]}  {Path(e['cwd']).name:<20} blocked {_t(e['blocked_at'])}  {st:<16} {when:<12} msg={e['message']!r}")
        lines.append(f"              ask: {e['last_user_message'][:90]}")
    return "\n".join(lines)


DEFAULT_PATH = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"


def build_plist(python: str, workdir: str, data_dir: str, path: str = DEFAULT_PATH) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>-m</string>
        <string>codex_autoresume</string>
        <string>run</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{workdir}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{data_dir}/daemon.log</string>
    <key>StandardErrorPath</key>
    <string>{data_dir}/daemon.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{path}</string>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>
</dict>
</plist>
"""


def install_signal_handlers() -> None:
    def _stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _stop)


def install_path(env_path: str) -> str:
    """PATH for the launchd job: the user's real tool dirs first, then the defaults; temp shims dropped."""
    defaults = DEFAULT_PATH.split(":")
    extra = []
    for d in env_path.split(":"):
        if not d or d in defaults or d in extra:
            continue
        if d.startswith(("/var/folders", "/private/tmp", "/tmp", "/private/var/folders")):
            continue
        extra.append(d)
    return ":".join(extra + defaults)


def _wait_unloaded(uid: int, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = subprocess.run(["launchctl", "print", f"gui/{uid}/{LABEL}"], capture_output=True)
        if r.returncode != 0:
            return True
        time.sleep(0.5)
    return False


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def cmd_run(cfg: Config) -> None:
    install_signal_handlers()
    Path(cfg.data_dir).mkdir(parents=True, exist_ok=True)
    sched = Scheduler(cfg, State(Path(cfg.data_dir) / "state.json"))
    server = make_server(sched, port=cfg.port)
    threading.Thread(target=server.serve_forever, daemon=True, name="codex-autoresume-web").start()
    sched.log(f"web UI on http://127.0.0.1:{cfg.port}")
    try:
        sched.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        sched.stop()
        server.shutdown()


def cmd_status(cfg: Config) -> None:
    """Read-only: ask the running daemon; fall back to a dry-run scheduler that never delivers."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{cfg.port}/api/state", timeout=5) as r:
            print(format_status(json.load(r)))
            return
    except Exception:  # noqa: BLE001
        pass
    sched = Scheduler(cfg, State(Path(cfg.data_dir) / "state.json"), notifier=lambda t, x: None, dry_run=True)
    sched.tick()
    print(format_status(sched.snapshot()))
    sched.stop()


def cmd_install(cfg: Config) -> None:
    Path(cfg.data_dir).mkdir(parents=True, exist_ok=True)
    p = _plist_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True)
    if not _wait_unloaded(uid):
        print("warning: previous codex-autoresume job did not unload within 15s")
    p.write_text(build_plist(sys.executable, str(Path(__file__).resolve().parent.parent), cfg.data_dir,
                             install_path(os.environ.get("PATH", ""))))
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(p)], capture_output=True, text=True)
    print(f"installed {p}" if r.returncode == 0 else f"launchctl bootstrap failed: {r.stderr.strip()}")
    print(f"web UI: http://127.0.0.1:{cfg.port}   log: {cfg.data_dir}/daemon.log")


def cmd_uninstall(cfg: Config) -> None:
    p = _plist_path()
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    if p.exists():
        p.unlink()
    print("uninstalled")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="codex-autoresume")
    ap.add_argument("command", choices=["run", "status", "install-launchd", "uninstall-launchd"])
    ap.add_argument("--config", default=None)
    args = ap.parse_args(argv)
    cfg = Config.load(args.config)
    {"run": cmd_run, "status": cmd_status, "install-launchd": cmd_install, "uninstall-launchd": cmd_uninstall}[args.command](cfg)


if __name__ == "__main__":
    main()
