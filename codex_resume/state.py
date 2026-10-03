from __future__ import annotations
import json
import os
import threading
from pathlib import Path

HISTORY_CAP = 200
_DEFAULT_THREAD = {"skip": False, "message": None, "model": None, "effort": None, "attempts": [],
                   "resume_now": False, "pending_in_app_at": None, "pending_turn_id": None, "last_result": None}


class State:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.data = {"threads": {}, "history": []}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text())
                if isinstance(loaded, dict):
                    self.data["threads"] = dict(loaded.get("threads") or {})
                    self.data["history"] = list(loaded.get("history") or [])
            except (json.JSONDecodeError, OSError):
                pass

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=1))
            os.replace(tmp, self.path)

    def thread(self, tid: str) -> dict:
        with self._lock:
            t = self.data["threads"].setdefault(tid, {})
            for k, v in _DEFAULT_THREAD.items():
                t.setdefault(k, list(v) if isinstance(v, list) else v)
            return t

    def _set(self, tid: str, **kv) -> None:
        with self._lock:
            self.thread(tid).update(kv)
            self.save()

    def set_skip(self, tid: str, flag: bool) -> None:
        self._set(tid, skip=bool(flag))

    def set_message(self, tid: str, message: str | None, model: str | None = None, effort: str | None = None) -> None:
        self._set(tid, message=message or None, model=model or None, effort=effort or None)

    def set_resume_now(self, tid: str, flag: bool) -> None:
        self._set(tid, resume_now=bool(flag))

    def set_pending_in_app(self, tid: str, ts: int | None, turn_id: str | None = None) -> None:
        self._set(tid, pending_in_app_at=ts, pending_turn_id=turn_id if ts else None)

    def set_last_result(self, tid: str, text: str | None) -> None:
        self._set(tid, last_result=text)

    def record_attempt(self, tid: str, ts: int) -> None:
        with self._lock:
            self.thread(tid)["attempts"].append(int(ts))
            self.save()

    def attempts_since(self, tid: str, since_ts: int) -> int:
        return sum(1 for a in self.thread(tid)["attempts"] if a >= since_ts)

    def add_history(self, ts: int, tid: str | None, event: str, detail: str = "") -> None:
        with self._lock:
            self.data["history"].append({"ts": int(ts), "thread_id": tid, "event": event, "detail": detail[:500]})
            self.data["history"] = self.data["history"][-HISTORY_CAP:]
            self.save()

    def history(self, n: int = 50) -> list[dict]:
        with self._lock:
            return list(reversed(self.data["history"][-n:]))
