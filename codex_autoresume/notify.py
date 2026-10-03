from __future__ import annotations
import json
import subprocess


def _quote(s: str) -> str:
    return json.dumps("".join(ch for ch in s if ch >= " "), ensure_ascii=False)


def notify(title: str, text: str, runner=subprocess.run) -> bool:
    script = f"display notification {_quote(text[:200])} with title {_quote(title)}"
    try:
        r = runner(["/usr/bin/osascript", "-e", script], capture_output=True, timeout=10)
        return getattr(r, "returncode", 1) == 0
    except Exception:  # noqa: BLE001
        return False
