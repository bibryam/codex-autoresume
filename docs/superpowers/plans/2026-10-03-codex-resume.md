# codex-resume Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local macOS daemon plus web page that detects Codex threads stopped on `usageLimitExceeded`, shows when each will resume, and continues them one at a time once the 5-hour window resets.

**Architecture:** A 60-second tick loop reads limits through our own `codex app-server` engine (JSON-RPC over stdio), finds blocked threads by reading Codex's sqlite databases read-only, builds a plan with a pure function, and delivers "continue" either by resuming the thread in our engine (`thread/resume` + `turn/start`) or, when the desktop holds the thread's writer lock, by `codex queue` and waiting for the desktop to drain it. A stdlib `http.server` serves a single page polling `/api/state`.

**Tech Stack:** Python 3.14 standard library only (`subprocess`, `sqlite3`, `http.server`, `threading`, `unittest`). macOS `osascript` for notifications, `launchd` for autostart.

**Spec:** `docs/superpowers/specs/2026-10-03-codex-resume-design.md`

## Global Constraints

- No third-party Python packages. Tests run with `python3 -m unittest discover -s tests -v`.
- Codex binary default: `/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex`; fall back to `codex` on PATH.
- Never write to any file under `~/.codex`; the only write path is the `codex queue` CLI.
- Web server binds `127.0.0.1` only, default port 8765.
- All timestamps are epoch seconds in data; the page renders local time.
- Defaults: `weekly_guard` 90, `max_attempts` 3 per 24h, `grace_seconds` 60, `default_message` "continue", `approvals` "guardian", `desktop_wait_seconds` 90, `turn_timeout_seconds` 14400.
- Commits use `git commit -s`, no Co-Authored-By line, no push.

## Review Focus

1. Limits unreadable (engine down) while threads are blocked: nothing must be delivered; the plan shows the reset parsed from the error message. Test in Task 6 (`test_plan_without_limits_uses_hint`).
2. A thread the user already continued by hand (newer completed turn) must disappear from the queue. Test in Task 4 (`test_ignores_thread_with_later_successful_turn`).
3. The resumed turn fails again with `usageLimitExceeded`: the thread must be re-queued after the next reset, not retried in a loop. Test in Task 6 (`test_reblocked_thread_waits_for_new_reset`) and Task 8 (`test_deliver_engine_turn_failed_usage_limit`).
4. Desktop holds the lock and never drains the queue: exactly one `codex queue` call, state `pending-in-app`, no second queue on the next tick. Test in Task 8 (`test_deliver_pending_in_app`) and Task 6 (`test_pending_in_app_not_rescheduled`).
5. Message with quotes or non-ASCII typed in the UI must reach `codex queue` and `turn/start` unchanged. Test in Task 8 (`test_queue_message_passes_text_verbatim`) and Task 10 (`test_post_message_roundtrip`).

---

## File structure

```
codex_resume/
  __init__.py      version string
  config.py        Config dataclass, load() from ~/.codex-resume/config.json
  engine.py        Engine: JSON-RPC client over `codex app-server --listen stdio://`
  limits.py        Window/Limits dataclasses, parse_limits, read_limits, parse_reset_hint
  detector.py      Blocked dataclass, find_blocked() over state_5.sqlite + thread_history_1.sqlite
  state.py         State: per-thread flags, attempts, history (json file)
  planner.py       Entry/Plan dataclasses, build_plan() (pure)
  deliver.py       queue_message(), latest_turn(), deliver()
  notify.py        notify() via osascript
  scheduler.py     Scheduler: tick loop, delivery worker, snapshot for the web
  web.py           make_server(): page + /api routes
  __main__.py      CLI: run, status, install-launchd, uninstall-launchd
tests/
  fake_codex.py    fake app-server used by test_engine
  test_limits.py test_detector.py test_state.py test_planner.py test_engine.py
  test_deliver.py test_scheduler.py test_web.py
README.md
```

---

### Task 1: Package skeleton and Config

**Files:**
- Create: `codex_resume/__init__.py`, `codex_resume/config.py`, `tests/test_config.py`, `.gitignore`

**Interfaces:**
- Produces: `Config` dataclass with fields `codex_bin, codex_home, data_dir, port, weekly_guard, max_attempts, approvals, default_message, grace_seconds, tick_seconds, desktop_wait_seconds, turn_timeout_seconds`; `Config.load(path: str | None = None) -> Config`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config.py
import json, tempfile, unittest
from pathlib import Path
from codex_resume.config import Config

class ConfigTest(unittest.TestCase):
    def test_defaults(self):
        cfg = Config()
        self.assertEqual(cfg.port, 8765)
        self.assertEqual(cfg.weekly_guard, 90)
        self.assertEqual(cfg.default_message, "continue")

    def test_load_overrides_known_keys_only(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.json"
            p.write_text(json.dumps({"port": 9000, "bogus": 1, "weekly_guard": 50}))
            cfg = Config.load(str(p))
            self.assertEqual(cfg.port, 9000)
            self.assertEqual(cfg.weekly_guard, 50)
            self.assertFalse(hasattr(cfg, "bogus"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_config -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'codex_resume'`

- [ ] **Step 3: Write the implementation**

```python
# codex_resume/__init__.py
__version__ = "0.1.0"
```

```python
# codex_resume/config.py
from __future__ import annotations
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BIN = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"


@dataclass
class Config:
    codex_bin: str = DEFAULT_BIN
    codex_home: str = str(Path.home() / ".codex")
    data_dir: str = str(Path.home() / ".codex-resume")
    port: int = 8765
    weekly_guard: int = 90
    max_attempts: int = 3
    approvals: str = "guardian"  # "guardian" or "decline"
    default_message: str = "continue"
    grace_seconds: int = 60
    tick_seconds: int = 60
    desktop_wait_seconds: int = 90
    turn_timeout_seconds: int = 14400

    @classmethod
    def load(cls, path: str | None = None) -> "Config":
        cfg = cls()
        p = Path(path) if path else Path(cfg.data_dir) / "config.json"
        if p.exists():
            for key, value in json.loads(p.read_text()).items():
                if key in cfg.__dataclass_fields__:
                    setattr(cfg, key, value)
        if not Path(cfg.codex_bin).exists():
            found = shutil.which("codex")
            if found:
                cfg.codex_bin = found
        return cfg
```

```
# .gitignore
__pycache__/
*.pyc
.DS_Store
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_config -v`
Expected: 2 tests PASS

- [ ] **Step 5: Commit**

```bash
git add codex_resume/__init__.py codex_resume/config.py tests/test_config.py .gitignore
git commit -s -m "Add codex_resume package skeleton and Config"
```

---

### Task 2: Limits parsing and reset-hint parsing

**Files:**
- Create: `codex_resume/limits.py`, `tests/test_limits.py`

**Interfaces:**
- Produces: `Window(used_percent: int, resets_at: int | None, window_minutes: int | None)`, `Limits(primary, secondary, allowed: bool | None, plan: str | None, fetched_at: int)` with `to_dict()`, `parse_limits(result: dict, now: int) -> Limits`, `read_limits(engine, now: int | None = None) -> Limits` (calls `engine.call("account/rateLimits/read", {})`), `parse_reset_hint(message: str, reference_ts: int, tz=None) -> int | None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_limits.py
import unittest
from datetime import datetime, timezone, timedelta
from codex_resume.limits import parse_limits, parse_reset_hint, Limits

SAMPLE = {
    "ordinaryUsageAllowed": False,
    "rateLimits": {
        "planType": "plus",
        "primary": {"usedPercent": 100, "resetsAt": 1791032178, "windowDurationMins": 300},
        "secondary": {"usedPercent": 20, "resetsAt": 1791581316, "windowDurationMins": 10080},
        "rateLimitReachedType": {"type": "rate_limit_reached"},
    },
}

class LimitsTest(unittest.TestCase):
    def test_parse_limits(self):
        lim = parse_limits(SAMPLE, now=1791018000)
        self.assertFalse(lim.allowed)
        self.assertEqual(lim.primary.used_percent, 100)
        self.assertEqual(lim.primary.resets_at, 1791032178)
        self.assertEqual(lim.secondary.window_minutes, 10080)
        self.assertEqual(lim.plan, "plus")
        self.assertEqual(lim.to_dict()["primary"]["resets_at"], 1791032178)

    def test_parse_limits_from_notification_without_allowed_flag(self):
        res = {"rateLimits": {"primary": {"usedPercent": 10, "resetsAt": 5}, "rateLimitReachedType": None}}
        self.assertTrue(parse_limits(res, now=1).allowed)
        res["rateLimits"]["rateLimitReachedType"] = {"type": "rate_limit_reached"}
        self.assertFalse(parse_limits(res, now=1).allowed)

    def test_parse_limits_missing_windows(self):
        lim = parse_limits({}, now=7)
        self.assertIsNone(lim.primary)
        self.assertIsNone(lim.allowed)
        self.assertEqual(lim.fetched_at, 7)

    def test_reset_hint_same_day(self):
        tz = timezone(timedelta(hours=1))
        ref = int(datetime(2026, 10, 3, 9, 30, tzinfo=tz).timestamp())
        msg = "You've hit your usage limit. ... or try again at 1:56 PM."
        got = parse_reset_hint(msg, ref, tz=tz)
        self.assertEqual(got, int(datetime(2026, 10, 3, 13, 56, tzinfo=tz).timestamp()))

    def test_reset_hint_rolls_to_next_day(self):
        tz = timezone(timedelta(hours=1))
        ref = int(datetime(2026, 10, 3, 22, 30, tzinfo=tz).timestamp())
        got = parse_reset_hint("try again at 3:30 AM.", ref, tz=tz)
        self.assertEqual(got, int(datetime(2026, 10, 4, 3, 30, tzinfo=tz).timestamp()))

    def test_reset_hint_absent(self):
        self.assertIsNone(parse_reset_hint("some other error", 0))
        self.assertIsNone(parse_reset_hint("", 0))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_limits -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'codex_resume.limits'`

- [ ] **Step 3: Write the implementation**

```python
# codex_resume/limits.py
from __future__ import annotations
import re
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta


@dataclass
class Window:
    used_percent: int
    resets_at: int | None
    window_minutes: int | None


@dataclass
class Limits:
    primary: Window | None
    secondary: Window | None
    allowed: bool | None
    plan: str | None
    fetched_at: int

    def to_dict(self) -> dict:
        return {
            "primary": asdict(self.primary) if self.primary else None,
            "secondary": asdict(self.secondary) if self.secondary else None,
            "allowed": self.allowed,
            "plan": self.plan,
            "fetched_at": self.fetched_at,
        }


def _window(d: dict | None) -> Window | None:
    if not d:
        return None
    return Window(int(d.get("usedPercent") or 0), d.get("resetsAt"), d.get("windowDurationMins"))


def parse_limits(result: dict, now: int) -> Limits:
    rl = result.get("rateLimits") or {}
    allowed = result.get("ordinaryUsageAllowed")
    if allowed is None and rl:
        allowed = rl.get("rateLimitReachedType") is None
    plan = rl.get("planType")
    if not isinstance(plan, str):
        plan = None
    return Limits(_window(rl.get("primary")), _window(rl.get("secondary")), allowed, plan, now)


def read_limits(engine, now: int | None = None) -> Limits:
    result = engine.call("account/rateLimits/read", {})
    return parse_limits(result or {}, now if now is not None else int(time.time()))


_RESET_RE = re.compile(r"try again at (\d{1,2}):(\d{2})\s*(AM|PM)", re.IGNORECASE)


def parse_reset_hint(message: str, reference_ts: int, tz=None) -> int | None:
    """Turn 'try again at 1:56 PM' into an epoch, relative to the day of reference_ts (local time)."""
    m = _RESET_RE.search(message or "")
    if not m:
        return None
    hour, minute, ampm = int(m.group(1)) % 12, int(m.group(2)), m.group(3).upper()
    if ampm == "PM":
        hour += 12
    ref = datetime.fromtimestamp(reference_ts, tz)
    cand = ref.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if cand < ref:
        cand += timedelta(days=1)
    return int(cand.timestamp())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_limits -v`
Expected: 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add codex_resume/limits.py tests/test_limits.py
git commit -s -m "Add limits parsing and reset-hint parsing"
```

---

### Task 3: Engine JSON-RPC client

**Files:**
- Create: `codex_resume/engine.py`, `tests/fake_codex.py`, `tests/test_engine.py`

**Interfaces:**
- Produces: `EngineError(code: int, message: str)`; `Engine(codex_bin: str, client_name="codex-resume", on_notification: Callable[[str, dict], None] | None = None, on_server_request: Callable[[str, dict], tuple[dict | None, dict | None]] | None = None)` with `start()`, `alive() -> bool`, `call(method, params=None, timeout=60) -> dict | None`, `notify(method, params)`, `stop()`, and static `default_server_request(method, params) -> (result, error)` which declines approvals.
- Protocol facts: newline-delimited JSON-RPC 2.0; after `initialize` the client sends notification `initialized`; server requests are messages with both `id` and `method` and must be answered with `{"id", "result"}` or `{"id", "error"}`.

- [ ] **Step 1: Write the fake server and the failing test**

```python
# tests/fake_codex.py
"""Minimal stand-in for `codex app-server --listen stdio://` used by tests."""
import json
import sys


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if method == "initialize":
            out = {"id": mid, "result": {"userAgent": "fake/0", "codexHome": "/tmp"}}
        elif method == "initialized":
            continue
        elif method == "echo":
            out = {"id": mid, "result": params}
        elif method == "boom":
            out = {"id": mid, "error": {"code": -32600, "message": "thread x already has an active writer"}}
        elif method == "emit":
            sys.stdout.write(json.dumps({"method": "turn/completed", "params": {"threadId": "t1", "turn": {"id": "u1", "status": "completed"}}}) + "\n")
            out = {"id": mid, "result": {}}
        elif method == "ask":
            sys.stdout.write(json.dumps({"id": 900, "method": "item/commandExecution/requestApproval", "params": {"threadId": "t1", "command": "rm -rf"}}) + "\n")
            sys.stdout.flush()
            reply = json.loads(sys.stdin.readline())
            out = {"id": mid, "result": {"gotDecision": (reply.get("result") or {}).get("decision"), "gotError": reply.get("error")}}
        elif method == "exit":
            return
        else:
            out = {"id": mid, "error": {"code": -32601, "message": "unknown"}}
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
```

```python
# tests/test_engine.py
import os, sys, unittest
from codex_resume.engine import Engine, EngineError

FAKE = os.path.join(os.path.dirname(__file__), "fake_codex.py")


class FakeBinEngine(Engine):
    def _command(self):
        return [sys.executable, FAKE]


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.notes = []
        self.eng = FakeBinEngine("unused", on_notification=lambda m, p: self.notes.append((m, p)))
        self.eng.start()

    def tearDown(self):
        self.eng.stop()

    def test_call_roundtrip(self):
        self.assertEqual(self.eng.call("echo", {"a": 1}), {"a": 1})
        self.assertTrue(self.eng.alive())

    def test_error_raises(self):
        with self.assertRaises(EngineError) as cm:
            self.eng.call("boom", {})
        self.assertIn("active writer", cm.exception.message)
        self.assertEqual(cm.exception.code, -32600)

    def test_notification_dispatch(self):
        self.eng.call("emit", {})
        self.eng.call("echo", {})  # ordering barrier
        self.assertIn(("turn/completed", {"threadId": "t1", "turn": {"id": "u1", "status": "completed"}}), self.notes)

    def test_server_request_declined_by_default(self):
        res = self.eng.call("ask", {})
        self.assertEqual(res["gotDecision"], "decline")

    def test_server_request_custom_handler(self):
        eng = FakeBinEngine("unused", on_server_request=lambda m, p: ({"decision": "accept"}, None))
        eng.start()
        try:
            self.assertEqual(eng.call("ask", {})["gotDecision"], "accept")
        finally:
            eng.stop()

    def test_call_after_exit_raises(self):
        self.eng.call("exit", {}) if False else self.eng.notify("exit", {})
        import time; time.sleep(0.3)
        with self.assertRaises(EngineError):
            self.eng.call("echo", {}, timeout=1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_engine -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'codex_resume.engine'`

- [ ] **Step 3: Write the implementation**

```python
# codex_resume/engine.py
from __future__ import annotations
import itertools
import json
import subprocess
import threading
from typing import Callable

from . import __version__

Notification = Callable[[str, dict], None]
ServerRequest = Callable[[str, dict], tuple[dict | None, dict | None]]


class EngineError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class Engine:
    """JSON-RPC client for `codex app-server --listen stdio://`."""

    def __init__(self, codex_bin: str, client_name: str = "codex-resume",
                 on_notification: Notification | None = None,
                 on_server_request: ServerRequest | None = None):
        self.codex_bin = codex_bin
        self.client_name = client_name
        self.on_notification = on_notification
        self.on_server_request = on_server_request or self.default_server_request
        self.proc: subprocess.Popen | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, tuple[threading.Event, list]] = {}
        self._lock = threading.Lock()
        self._wlock = threading.Lock()

    def _command(self) -> list[str]:
        return [self.codex_bin, "app-server", "--listen", "stdio://"]

    def start(self) -> None:
        self.proc = subprocess.Popen(self._command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, bufsize=1)
        threading.Thread(target=self._read_loop, daemon=True, name="codex-engine-reader").start()
        self.call("initialize", {"clientInfo": {"name": self.client_name, "version": __version__}}, timeout=60)
        self.notify("initialized", {})

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _write(self, obj: dict) -> None:
        if not self.alive():
            raise EngineError(-2, "engine not running")
        line = json.dumps(obj) + "\n"
        with self._wlock:
            try:
                self.proc.stdin.write(line)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                raise EngineError(-2, f"engine pipe closed: {e}")

    def call(self, method: str, params: dict | None = None, timeout: float = 60):
        mid = next(self._ids)
        ev, slot = threading.Event(), []
        with self._lock:
            self._pending[mid] = (ev, slot)
        try:
            self._write({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})
            if not ev.wait(timeout):
                raise EngineError(-1, f"timeout waiting for {method}")
        finally:
            with self._lock:
                self._pending.pop(mid, None)
        msg = slot[0]
        if "error" in msg:
            err = msg["error"] or {}
            raise EngineError(int(err.get("code", -32000)), str(err.get("message", "unknown error")))
        return msg.get("result")

    def notify(self, method: str, params: dict | None = None) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def _respond(self, mid, result=None, error=None) -> None:
        out = {"jsonrpc": "2.0", "id": mid}
        if error is not None:
            out["error"] = error
        else:
            out["result"] = result if result is not None else {}
        try:
            self._write(out)
        except EngineError:
            pass

    def _read_loop(self) -> None:
        proc = self.proc
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "method" in msg and "id" in msg:
                    try:
                        result, error = self.on_server_request(msg["method"], msg.get("params") or {})
                    except Exception as e:  # noqa: BLE001
                        result, error = None, {"code": -32603, "message": str(e)}
                    self._respond(msg["id"], result, error)
                elif "id" in msg:
                    with self._lock:
                        entry = self._pending.get(msg["id"])
                    if entry:
                        entry[1].append(msg)
                        entry[0].set()
                elif "method" in msg and self.on_notification:
                    try:
                        self.on_notification(msg["method"], msg.get("params") or {})
                    except Exception:  # noqa: BLE001
                        pass
        finally:
            with self._lock:
                pending = list(self._pending.values())
                self._pending.clear()
            for ev, slot in pending:
                slot.append({"error": {"code": -2, "message": "engine exited"}})
                ev.set()

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                self.proc.kill()
        self.proc = None

    @staticmethod
    def default_server_request(method: str, params: dict) -> tuple[dict | None, dict | None]:
        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            return {"decision": "decline"}, None
        return None, {"code": -32601, "message": f"{method} is not handled by codex-resume"}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_engine -v`
Expected: 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add codex_resume/engine.py tests/fake_codex.py tests/test_engine.py
git commit -s -m "Add app-server JSON-RPC engine client"
```

---

### Task 4: Detector over Codex sqlite databases

**Files:**
- Create: `codex_resume/detector.py`, `tests/test_detector.py`

**Interfaces:**
- Produces: `Blocked(thread_id, cwd, title, last_user_message, blocked_at: int, reset_hint: str | None, model, effort, source, last_turn_id)` with `to_dict()`; `find_blocked(codex_home: str, limit: int = 50) -> list[Blocked]`; `latest_turn(codex_home, thread_id) -> tuple[turn_id, status, started_at, completed_at, error_info] | None`.
- Database facts (verified on CLI 0.160.0): `state_5.sqlite` table `threads(id, cwd, title, first_user_message, model, reasoning_effort, source, thread_source, archived, updated_at)`; main threads have `source` in `vscode|cli|exec` and `thread_source` `user` or NULL; subagents have JSON in `source` and `thread_source` `guardian_review|subagent`. `thread_history_1.sqlite` table `thread_turns(thread_id, turn_id, rollout_ordinal, status, error_json, started_at, completed_at)` with status `completed|failed|inProgress|interrupted` and `error_json` like `{"message": "... try again at 1:56 PM.", "codexErrorInfo": "usageLimitExceeded"}`; table `thread_items(thread_id, turn_id, rollout_ordinal, item_type, item_json)` where a user message is `{"type":"userMessage","content":[{"type":"text","text":"continue"}]}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_detector.py
import json, sqlite3, tempfile, unittest
from pathlib import Path
from codex_resume.detector import find_blocked, latest_turn

ERR = json.dumps({"message": "You've hit your usage limit. ... try again at 1:56 PM.", "codexErrorInfo": "usageLimitExceeded"})


def make_home(d: str) -> str:
    s = sqlite3.connect(Path(d) / "state_5.sqlite")
    s.execute("""create table threads (id text primary key, cwd text, title text, first_user_message text, model text,
                 reasoning_effort text, source text, thread_source text, archived int default 0, updated_at int)""")
    h = sqlite3.connect(Path(d) / "thread_history_1.sqlite")
    h.execute("create table thread_turns (thread_id text, turn_id text, rollout_ordinal int, status text, error_json text, started_at int, completed_at int)")
    h.execute("create table thread_items (thread_id text, turn_id text, rollout_ordinal int, item_type text, item_json text)")
    s.commit(); h.commit()
    return d


def add_thread(d, tid, turns, source="vscode", tsrc="user", archived=0, title="T", updated=100):
    s = sqlite3.connect(Path(d) / "state_5.sqlite")
    s.execute("insert into threads values (?,?,?,?,?,?,?,?,?,?)", (tid, "/p/proj", title, "first", "gpt-6-astra", "xhigh", source, tsrc, archived, updated))
    s.commit()
    h = sqlite3.connect(Path(d) / "thread_history_1.sqlite")
    for i, (status, err, user) in enumerate(turns, start=1):
        h.execute("insert into thread_turns values (?,?,?,?,?,?,?)", (tid, f"{tid}-turn{i}", i * 10, status, err, 1000 + i, 2000 + i))
        if user is not None:
            h.execute("insert into thread_items values (?,?,?,?,?)", (tid, f"{tid}-turn{i}", i * 10 + 1, "userMessage",
                      json.dumps({"type": "userMessage", "content": [{"type": "text", "text": user}]})))
    h.commit()


class DetectorTest(unittest.TestCase):
    def test_finds_blocked_main_thread_only(self):
        with tempfile.TemporaryDirectory() as d:
            make_home(d)
            add_thread(d, "main-blocked", [("completed", None, "do X"), ("failed", ERR, "continue")])
            add_thread(d, "main-ok", [("completed", None, "fine")])
            add_thread(d, "sub-blocked", [("failed", ERR, "judge")], source='{"subagent":{"other":"guardian"}}', tsrc="guardian_review")
            add_thread(d, "archived", [("failed", ERR, "x")], archived=1)
            add_thread(d, "other-fail", [("failed", json.dumps({"message": "boom", "codexErrorInfo": "internalServerError"}), "x")])
            got = find_blocked(d)
            self.assertEqual([b.thread_id for b in got], ["main-blocked"])
            b = got[0]
            self.assertEqual(b.last_user_message, "continue")
            self.assertEqual(b.blocked_at, 2002)
            self.assertIn("1:56 PM", b.reset_hint)
            self.assertEqual((b.model, b.effort, b.cwd, b.title), ("gpt-6-astra", "xhigh", "/p/proj", "T"))
            self.assertEqual(b.last_turn_id, "main-blocked-turn2")
            self.assertEqual(b.to_dict()["thread_id"], "main-blocked")

    def test_ignores_thread_with_later_successful_turn(self):
        with tempfile.TemporaryDirectory() as d:
            make_home(d)
            add_thread(d, "t", [("failed", ERR, "a"), ("completed", None, "continue")])
            self.assertEqual(find_blocked(d), [])

    def test_in_progress_turn_is_not_blocked(self):
        with tempfile.TemporaryDirectory() as d:
            make_home(d)
            add_thread(d, "t", [("failed", ERR, "a"), ("inProgress", None, "continue")])
            self.assertEqual(find_blocked(d), [])

    def test_latest_turn(self):
        with tempfile.TemporaryDirectory() as d:
            make_home(d)
            add_thread(d, "t", [("completed", None, "a"), ("failed", ERR, "b")])
            self.assertEqual(latest_turn(d, "t"), ("t-turn2", "failed", 1002, 2002, "usageLimitExceeded"))
            self.assertIsNone(latest_turn(d, "nope"))

    def test_missing_databases(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(find_blocked(d), [])
            self.assertIsNone(latest_turn(d, "t"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_detector -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'codex_resume.detector'`

- [ ] **Step 3: Write the implementation**

```python
# codex_resume/detector.py
from __future__ import annotations
import json
import sqlite3
from dataclasses import dataclass, asdict
from pathlib import Path

MAIN_SOURCES = ("vscode", "cli", "exec", "appserver", "appServer")


@dataclass
class Blocked:
    thread_id: str
    cwd: str
    title: str
    last_user_message: str
    blocked_at: int
    reset_hint: str | None
    model: str | None
    effort: str | None
    source: str
    last_turn_id: str

    def to_dict(self) -> dict:
        return asdict(self)


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)


def _user_text(item_json: str) -> str:
    try:
        item = json.loads(item_json)
    except json.JSONDecodeError:
        return ""
    return " ".join(c.get("text", "") for c in item.get("content", []) if c.get("type") == "text").strip()


def _error_info(error_json: str | None) -> tuple[str | None, str | None]:
    if not error_json:
        return None, None
    try:
        err = json.loads(error_json)
    except json.JSONDecodeError:
        return None, None
    return err.get("codexErrorInfo"), err.get("message")


def latest_turn(codex_home: str, thread_id: str):
    hist = Path(codex_home) / "thread_history_1.sqlite"
    if not hist.exists():
        return None
    with _ro(hist) as h:
        row = h.execute(
            "select turn_id, status, started_at, completed_at, error_json from thread_turns "
            "where thread_id=? order by rollout_ordinal desc limit 1", (thread_id,)).fetchone()
    if not row:
        return None
    info, _ = _error_info(row[4])
    return (row[0], row[1], row[2], row[3], info)


def find_blocked(codex_home: str, limit: int = 50) -> list[Blocked]:
    state = Path(codex_home) / "state_5.sqlite"
    hist = Path(codex_home) / "thread_history_1.sqlite"
    if not state.exists() or not hist.exists():
        return []
    placeholders = ",".join("?" * len(MAIN_SOURCES))
    out: list[Blocked] = []
    with _ro(state) as s, _ro(hist) as h:
        rows = s.execute(
            f"select id, cwd, title, first_user_message, model, reasoning_effort, source from threads "
            f"where archived=0 and source in ({placeholders}) and (thread_source is null or thread_source='user') "
            f"order by updated_at desc limit ?", (*MAIN_SOURCES, limit)).fetchall()
        for tid, cwd, title, first_msg, model, effort, source in rows:
            turn = h.execute(
                "select turn_id, status, error_json, started_at, completed_at from thread_turns "
                "where thread_id=? order by rollout_ordinal desc limit 1", (tid,)).fetchone()
            if not turn or turn[1] != "failed":
                continue
            info, message = _error_info(turn[2])
            if info != "usageLimitExceeded":
                continue
            item = h.execute(
                "select item_json from thread_items where thread_id=? and item_type='userMessage' "
                "order by rollout_ordinal desc limit 1", (tid,)).fetchone()
            out.append(Blocked(
                thread_id=tid, cwd=cwd or "", title=(title or first_msg or "").strip()[:200],
                last_user_message=(_user_text(item[0]) if item else "")[:300],
                blocked_at=int(turn[4] or turn[3] or 0), reset_hint=message, model=model, effort=effort,
                source=source or "", last_turn_id=turn[0]))
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_detector -v`
Expected: 5 tests PASS

- [ ] **Step 5: Smoke against the real databases**

Run: `python3 -c "from codex_resume.detector import find_blocked; [print(b.thread_id[:13], b.cwd.split('/')[-1], repr(b.last_user_message[:40]), b.blocked_at) for b in find_blocked('$HOME/.codex')]"`
Expected: the two cleverplayhouse threads `01a0fe2e-7ce2` and `01a0fe41-cd08` (unless the user already continued them).

- [ ] **Step 6: Commit**

```bash
git add codex_resume/detector.py tests/test_detector.py
git commit -s -m "Add blocked-thread detector over Codex sqlite state"
```

---

### Task 5: State file

**Files:**
- Create: `codex_resume/state.py`, `tests/test_state.py`

**Interfaces:**
- Produces: `State(path)` with `data` dict, `thread(tid) -> dict` (keys `skip, message, model, effort, attempts: list[int], resume_now, pending_in_app_at, last_result`), `set_skip(tid, flag)`, `set_message(tid, message, model=None, effort=None)`, `set_resume_now(tid, flag)`, `set_pending_in_app(tid, ts | None)`, `record_attempt(tid, ts)`, `attempts_since(tid, since_ts) -> int`, `add_history(ts, tid, event, detail="")`, `history(n=50) -> list[dict]` newest first, `save()`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_state.py
import tempfile, unittest
from pathlib import Path
from codex_resume.state import State

class StateTest(unittest.TestCase):
    def test_roundtrip_and_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "state.json"
            st = State(p)
            self.assertFalse(st.thread("a")["skip"])
            st.set_skip("a", True); st.set_message("a", "go on", model="m", effort="high")
            st.record_attempt("a", 100); st.record_attempt("a", 200)
            st.add_history(5, "a", "queued", "x")
            st2 = State(p)
            self.assertTrue(st2.thread("a")["skip"])
            self.assertEqual(st2.thread("a")["message"], "go on")
            self.assertEqual((st2.thread("a")["model"], st2.thread("a")["effort"]), ("m", "high"))
            self.assertEqual(st2.attempts_since("a", 150), 1)
            self.assertEqual(st2.attempts_since("a", 0), 2)
            self.assertEqual(st2.history()[0]["event"], "queued")

    def test_history_cap_and_order(self):
        with tempfile.TemporaryDirectory() as d:
            st = State(Path(d) / "s.json")
            for i in range(250):
                st.add_history(i, "t", f"e{i}")
            h = st.history(5)
            self.assertEqual([x["event"] for x in h], ["e249", "e248", "e247", "e246", "e245"])
            self.assertEqual(len(st.data["history"]), 200)

    def test_flags(self):
        with tempfile.TemporaryDirectory() as d:
            st = State(Path(d) / "s.json")
            st.set_resume_now("t", True); st.set_pending_in_app("t", 9)
            self.assertTrue(st.thread("t")["resume_now"]); self.assertEqual(st.thread("t")["pending_in_app_at"], 9)
            st.set_resume_now("t", False); st.set_pending_in_app("t", None)
            self.assertFalse(st.thread("t")["resume_now"]); self.assertIsNone(st.thread("t")["pending_in_app_at"])

    def test_corrupt_file_starts_fresh(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"; p.write_text("{not json")
            self.assertEqual(State(p).history(), [])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_state -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'codex_resume.state'`

- [ ] **Step 3: Write the implementation**

```python
# codex_resume/state.py
from __future__ import annotations
import json
import os
import threading
from pathlib import Path

HISTORY_CAP = 200
_DEFAULT_THREAD = {"skip": False, "message": None, "model": None, "effort": None, "attempts": [],
                   "resume_now": False, "pending_in_app_at": None, "last_result": None}


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

    def set_pending_in_app(self, tid: str, ts: int | None) -> None:
        self._set(tid, pending_in_app_at=ts)

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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_state -v`
Expected: 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add codex_resume/state.py tests/test_state.py
git commit -s -m "Add persistent state file"
```

---

### Task 6: Planner (pure)

**Files:**
- Create: `codex_resume/planner.py`, `tests/test_planner.py`

**Interfaces:**
- Consumes: `Blocked` (Task 4), `Limits` (Task 2), `State` (Task 5), `Config` (Task 1), `parse_reset_hint` (Task 2).
- Produces: `Entry(thread_id, title, cwd, last_user_message, blocked_at, resume_at: int | None, state: str, position: int | None, message, model, effort, attempts: int, reason: str)` with `to_dict()`; `Plan(entries: list[Entry], next_action: Entry | None, note: str)`; `build_plan(blocked, limits, state, cfg, now: int, running: str | None = None) -> Plan`.
- States: `running`, `skipped`, `pending-in-app`, `max-attempts`, `weekly-guard`, `scheduled`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_planner.py
import tempfile, unittest
from pathlib import Path
from codex_resume.config import Config
from codex_resume.detector import Blocked
from codex_resume.limits import Limits, Window
from codex_resume.planner import build_plan
from codex_resume.state import State

NOW = 1_000_000

def blocked(tid, at, hint="try again at 1:56 PM."):
    return Blocked(tid, "/p/x", "title " + tid, "last", at, hint, "gpt-6-astra", "xhigh", "vscode", tid + "-t")

def limits(allowed, p_reset=NOW + 3600, weekly=20):
    return Limits(Window(100 if not allowed else 10, p_reset, 300), Window(weekly, NOW + 99999, 10080), allowed, "plus", NOW)


class PlannerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.tmp.name) / "s.json")
        self.cfg = Config(grace_seconds=60, weekly_guard=90, max_attempts=3)

    def tearDown(self):
        self.tmp.cleanup()

    def test_not_allowed_schedules_after_reset_in_blocked_order(self):
        plan = build_plan([blocked("b", 200), blocked("a", 100)], limits(False), self.state, self.cfg, NOW)
        self.assertEqual([e.thread_id for e in plan.entries], ["a", "b"])
        self.assertEqual([e.position for e in plan.entries], [1, 2])
        self.assertEqual(plan.entries[0].resume_at, NOW + 3600 + 60)
        self.assertEqual(plan.entries[0].state, "scheduled")
        self.assertEqual(plan.entries[0].message, "continue")
        self.assertIsNone(plan.next_action)

    def test_allowed_and_due_picks_first(self):
        plan = build_plan([blocked("a", 100), blocked("b", 200)], limits(True), self.state, self.cfg, NOW)
        self.assertEqual(plan.next_action.thread_id, "a")
        self.assertEqual(plan.entries[0].resume_at, NOW)

    def test_running_blocks_next_action(self):
        plan = build_plan([blocked("a", 100), blocked("b", 200)], limits(True), self.state, self.cfg, NOW, running="a")
        self.assertEqual(plan.entries[0].state, "running")
        self.assertIsNone(plan.next_action)
        self.assertEqual(plan.entries[1].position, 1)

    def test_skip_weekly_guard_and_max_attempts(self):
        self.state.set_skip("s", True)
        for ts in (NOW - 100, NOW - 200, NOW - 300):
            self.state.record_attempt("m", ts)
        plan = build_plan([blocked("s", 1), blocked("m", 2), blocked("w", 3)], limits(True, weekly=95), self.state, self.cfg, NOW)
        by = {e.thread_id: e for e in plan.entries}
        self.assertEqual(by["s"].state, "skipped")
        self.assertEqual(by["m"].state, "max-attempts")
        self.assertEqual(by["w"].state, "weekly-guard")
        self.assertIsNone(plan.next_action)

    def test_resume_now_bypasses_guards_and_jumps_queue(self):
        for ts in (NOW - 100, NOW - 200, NOW - 300):
            self.state.record_attempt("m", ts)
        self.state.set_resume_now("m", True)
        plan = build_plan([blocked("a", 1), blocked("m", 2)], limits(False, weekly=95), self.state, self.cfg, NOW)
        self.assertEqual(plan.next_action.thread_id, "m")
        self.assertEqual(plan.entries[0].thread_id, "m")

    def test_plan_without_limits_uses_hint(self):
        import datetime as dt
        at = int(dt.datetime(2026, 10, 3, 9, 30).timestamp())
        now = at + 60
        plan = build_plan([blocked("a", at)], None, self.state, self.cfg, now)
        self.assertEqual(plan.entries[0].resume_at, int(dt.datetime(2026, 10, 3, 13, 56).timestamp()) + 60)
        self.assertIsNone(plan.next_action)
        self.assertIn("limits unavailable", plan.note)

    def test_reblocked_thread_waits_for_new_reset(self):
        # thread blocked again after the window reopened: limits say not allowed with a later reset
        plan = build_plan([blocked("a", NOW - 10)], limits(False, p_reset=NOW + 18000), self.state, self.cfg, NOW)
        self.assertEqual(plan.entries[0].resume_at, NOW + 18000 + 60)
        self.assertIsNone(plan.next_action)

    def test_pending_in_app_not_rescheduled(self):
        self.state.set_pending_in_app("a", NOW - 5)
        plan = build_plan([blocked("a", 1)], limits(True), self.state, self.cfg, NOW)
        self.assertEqual(plan.entries[0].state, "pending-in-app")
        self.assertIsNone(plan.next_action)

    def test_custom_message_model_effort(self):
        self.state.set_message("a", "carry on", model="gpt-x", effort="low")
        e = build_plan([blocked("a", 1)], limits(True), self.state, self.cfg, NOW).entries[0]
        self.assertEqual((e.message, e.model, e.effort), ("carry on", "gpt-x", "low"))
        self.assertEqual(e.to_dict()["message"], "carry on")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_planner -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'codex_resume.planner'`

- [ ] **Step 3: Write the implementation**

```python
# codex_resume/planner.py
from __future__ import annotations
from dataclasses import dataclass, asdict

from .config import Config
from .detector import Blocked
from .limits import Limits, parse_reset_hint
from .state import State

DAY = 86400


@dataclass
class Entry:
    thread_id: str
    title: str
    cwd: str
    last_user_message: str
    blocked_at: int
    resume_at: int | None
    state: str
    position: int | None
    message: str
    model: str | None
    effort: str | None
    attempts: int
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Plan:
    entries: list[Entry]
    next_action: Entry | None
    note: str


def _earliest(limits: Limits | None, b: Blocked, cfg: Config, now: int) -> tuple[int, str]:
    if limits is not None and limits.allowed:
        return now, "window open"
    if limits is not None and limits.allowed is False and limits.primary and limits.primary.resets_at:
        return int(limits.primary.resets_at) + cfg.grace_seconds, "after 5h window resets"
    hint = parse_reset_hint(b.reset_hint or "", b.blocked_at)
    if hint:
        return hint + cfg.grace_seconds, "from error message (limits unavailable)"
    return now + 300, "no reset time known, retry in 5 min"


def build_plan(blocked: list[Blocked], limits: Limits | None, state: State, cfg: Config,
               now: int, running: str | None = None) -> Plan:
    weekly = limits.secondary.used_percent if (limits and limits.secondary) else 0
    note = "" if limits is not None else "limits unavailable; using reset times from error messages"
    ordered = sorted(blocked, key=lambda b: (not state.thread(b.thread_id)["resume_now"], b.blocked_at))
    entries: list[Entry] = []
    position = 0
    for b in ordered:
        st = state.thread(b.thread_id)
        attempts = state.attempts_since(b.thread_id, now - DAY)
        resume_at, reason = _earliest(limits, b, cfg, now)
        if st["resume_now"]:
            resume_at, reason = now, "resume now requested"
        if running == b.thread_id:
            kind = "running"
        elif st["skip"]:
            kind = "skipped"
        elif st["pending_in_app_at"]:
            kind = "pending-in-app"; reason = "queued in the app; open the thread to start it"
        elif not st["resume_now"] and attempts >= cfg.max_attempts:
            kind = "max-attempts"; reason = f"{attempts} automatic resumes in 24h"
        elif not st["resume_now"] and weekly >= cfg.weekly_guard:
            kind = "weekly-guard"; reason = f"weekly usage {weekly}% >= guard {cfg.weekly_guard}%"
        else:
            kind = "scheduled"
        pos = None
        if kind == "scheduled":
            position += 1
            pos = position
        entries.append(Entry(
            thread_id=b.thread_id, title=b.title, cwd=b.cwd, last_user_message=b.last_user_message,
            blocked_at=b.blocked_at, resume_at=resume_at if kind in ("scheduled", "running") else None,
            state=kind, position=pos, message=st["message"] or cfg.default_message,
            model=st["model"] or b.model, effort=st["effort"] or b.effort, attempts=attempts, reason=reason))
    next_action = None
    if running is None:
        for e in entries:
            if e.state != "scheduled" or e.resume_at is None or e.resume_at > now:
                continue
            forced = state.thread(e.thread_id)["resume_now"]
            if forced or (limits is not None and limits.allowed):
                next_action = e
            break
    return Plan(entries, next_action, note)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_planner -v`
Expected: 9 tests PASS

- [ ] **Step 5: Commit**

```bash
git add codex_resume/planner.py tests/test_planner.py
git commit -s -m "Add pure planner for resume scheduling"
```

---

### Task 7: Notifications

**Files:**
- Create: `codex_resume/notify.py`, `tests/test_notify.py`

**Interfaces:**
- Produces: `notify(title: str, text: str, runner=subprocess.run) -> bool`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_notify.py
import unittest
from codex_resume.notify import notify

class NotifyTest(unittest.TestCase):
    def test_builds_osascript_command(self):
        calls = []
        def runner(cmd, **kw):
            calls.append(cmd); return type("R", (), {"returncode": 0})()
        self.assertTrue(notify("codex-resume", 'He said "hi" – ünïcode', runner=runner))
        self.assertEqual(calls[0][:2], ["/usr/bin/osascript", "-e"])
        self.assertIn('display notification "He said \\"hi\\" – ünïcode" with title "codex-resume"', calls[0][2])

    def test_failure_is_swallowed(self):
        def runner(cmd, **kw):
            raise OSError("no osascript")
        self.assertFalse(notify("t", "x", runner=runner))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_notify -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the implementation**

```python
# codex_resume/notify.py
from __future__ import annotations
import json
import subprocess


def _quote(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def notify(title: str, text: str, runner=subprocess.run) -> bool:
    script = f"display notification {_quote(text[:200])} with title {_quote(title)}"
    try:
        r = runner(["/usr/bin/osascript", "-e", script], capture_output=True, timeout=10)
        return getattr(r, "returncode", 1) == 0
    except Exception:  # noqa: BLE001
        return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_notify -v`
Expected: 2 tests PASS

- [ ] **Step 5: Commit**

```bash
git add codex_resume/notify.py tests/test_notify.py
git commit -s -m "Add macOS notification helper"
```

---

### Task 8: Delivery

**Files:**
- Create: `codex_resume/deliver.py`, `tests/test_deliver.py`

**Interfaces:**
- Consumes: `Engine.call` / `EngineError` (Task 3), `latest_turn` (Task 4), `Entry` (Task 6), `Config` (Task 1).
- Produces: `Outcome(mode: str, ok: bool, detail: str, turn_status: str | None = None, error_info: str | None = None)`; `queue_message(cfg, thread_id, message, runner=subprocess.run) -> str`; `TurnWatch` protocol: object with `wait(timeout) -> dict | None` returning the completed turn; `deliver(cfg, engine, entry, begin_watch: Callable[[str], TurnWatch], log: Callable[[str], None], sleep=time.sleep, now=time.time, latest=latest_turn, queue=queue_message) -> Outcome`.
- Modes: `engine` (our engine ran the turn), `desktop` (queued, desktop ran it), `pending-in-app` (queued, nothing started), `error`.
- Protocol facts: `thread/resume` params `{threadId, excludeTurns: true, model?, config?: {"model_reasoning_effort": effort}, approvalsReviewer?: "guardian_subagent"}`; a thread loaded by the desktop returns error message containing `already has an active writer`; `turn/start` params `{threadId, input: [{"type": "text", "text": ...}], model?, effort?, approvalsReviewer?}` returns `{"turn": {"id": ...}}`; completion arrives as notification `turn/completed` with `turn.status` in `completed|failed|interrupted` and `turn.error.codexErrorInfo`; `thread/unsubscribe` params `{threadId}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_deliver.py
import unittest
from codex_resume.config import Config
from codex_resume.deliver import deliver, queue_message, Outcome
from codex_resume.engine import EngineError
from codex_resume.planner import Entry


def entry(**kw):
    base = dict(thread_id="t1", title="T", cwd="/p", last_user_message="x", blocked_at=1, resume_at=2, state="scheduled",
                position=1, message="continue", model="gpt-6-astra", effort="xhigh", attempts=0, reason="")
    base.update(kw)
    return Entry(**base)


class FakeEngine:
    def __init__(self, resume_error=None):
        self.calls = []
        self.resume_error = resume_error

    def call(self, method, params=None, timeout=60):
        self.calls.append((method, params))
        if method == "thread/resume" and self.resume_error:
            raise EngineError(-32600, self.resume_error)
        if method == "turn/start":
            return {"turn": {"id": "turn-9", "status": "inProgress"}}
        return {}


class Watch:
    def __init__(self, turn): self.turn = turn
    def wait(self, timeout): return self.turn


class DeliverTest(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(codex_bin="/bin/echo", codex_home="/nonexistent", desktop_wait_seconds=20)
        self.logs = []

    def test_deliver_engine_completed(self):
        eng = FakeEngine()
        watch = Watch({"id": "turn-9", "status": "completed"})
        out = deliver(self.cfg, eng, entry(), lambda tid: watch, self.logs.append)
        self.assertEqual((out.mode, out.ok, out.turn_status), ("engine", True, "completed"))
        methods = [m for m, _ in eng.calls]
        self.assertEqual(methods, ["thread/resume", "turn/start", "thread/unsubscribe"])
        resume = eng.calls[0][1]
        self.assertEqual(resume["model"], "gpt-6-astra")
        self.assertEqual(resume["config"], {"model_reasoning_effort": "xhigh"})
        self.assertEqual(resume["approvalsReviewer"], "guardian_subagent")
        self.assertTrue(resume["excludeTurns"])
        start = eng.calls[1][1]
        self.assertEqual(start["input"], [{"type": "text", "text": "continue"}])
        self.assertEqual((start["model"], start["effort"]), ("gpt-6-astra", "xhigh"))

    def test_deliver_engine_turn_failed_usage_limit(self):
        eng = FakeEngine()
        watch = Watch({"id": "turn-9", "status": "failed", "error": {"message": "limit", "codexErrorInfo": "usageLimitExceeded"}})
        out = deliver(self.cfg, eng, entry(), lambda tid: watch, self.logs.append)
        self.assertEqual((out.mode, out.ok, out.error_info), ("engine", False, "usageLimitExceeded"))
        self.assertEqual([m for m, _ in eng.calls][-1], "thread/unsubscribe")

    def test_deliver_engine_timeout(self):
        eng = FakeEngine()
        out = deliver(self.cfg, eng, entry(), lambda tid: Watch(None), self.logs.append)
        self.assertEqual((out.mode, out.ok), ("engine", False))
        self.assertIn("timed out", out.detail)

    def test_decline_approvals_config_omits_reviewer(self):
        eng = FakeEngine()
        cfg = Config(codex_bin="/bin/echo", approvals="decline")
        deliver(cfg, eng, entry(model=None, effort=None), lambda tid: Watch({"status": "completed"}), self.logs.append)
        self.assertNotIn("approvalsReviewer", eng.calls[0][1])
        self.assertNotIn("model", eng.calls[0][1])
        self.assertNotIn("config", eng.calls[0][1])

    def test_deliver_via_desktop_completed(self):
        eng = FakeEngine(resume_error="thread t1 already has an active writer")
        turns = iter([("old", "failed", 1, 2, "usageLimitExceeded"), ("new", "inProgress", 3, None, None),
                      ("new", "completed", 3, 4, None)])
        queued = []
        clock = [1000.0]
        def now(): return clock[0]
        def sleep(s): clock[0] += s
        out = deliver(self.cfg, eng, entry(), lambda tid: Watch(None), self.logs.append, sleep=sleep, now=now,
                      latest=lambda home, tid: next(turns), queue=lambda cfg, tid, msg: queued.append((tid, msg)) or "ok")
        self.assertEqual((out.mode, out.ok, out.turn_status), ("desktop", True, "completed"))
        self.assertEqual(queued, [("t1", "continue")])
        self.assertEqual([m for m, _ in eng.calls], ["thread/resume"])

    def test_deliver_pending_in_app(self):
        eng = FakeEngine(resume_error="thread t1 already has an active writer")
        queued = []
        clock = [1000.0]
        out = deliver(self.cfg, eng, entry(), lambda tid: Watch(None), self.logs.append,
                      sleep=lambda s: clock.__setitem__(0, clock[0] + s), now=lambda: clock[0],
                      latest=lambda home, tid: ("old", "failed", 1, 2, "usageLimitExceeded"),
                      queue=lambda cfg, tid, msg: queued.append(msg) or "ok")
        self.assertEqual((out.mode, out.ok), ("pending-in-app", False))
        self.assertEqual(len(queued), 1)

    def test_deliver_other_resume_error(self):
        eng = FakeEngine(resume_error="thread not found")
        out = deliver(self.cfg, eng, entry(), lambda tid: Watch(None), self.logs.append)
        self.assertEqual(out.mode, "error")
        self.assertIn("thread not found", out.detail)

    def test_queue_message_passes_text_verbatim(self):
        seen = []
        def runner(cmd, **kw):
            seen.append(cmd); return type("R", (), {"returncode": 0, "stdout": "Queued message 1 for thread t.\n", "stderr": ""})()
        msg = 'say "hi" — ünï'
        self.assertIn("Queued", queue_message(self.cfg, "t", msg, runner=runner))
        self.assertEqual(seen[0], ["/bin/echo", "queue", "--thread", "t", "--message", msg])

    def test_queue_message_failure_raises(self):
        def runner(cmd, **kw):
            return type("R", (), {"returncode": 1, "stdout": "", "stderr": "no such thread"})()
        with self.assertRaises(RuntimeError):
            queue_message(self.cfg, "t", "x", runner=runner)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_deliver -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'codex_resume.deliver'`

- [ ] **Step 3: Write the implementation**

```python
# codex_resume/deliver.py
from __future__ import annotations
import subprocess
import time
from dataclasses import dataclass
from typing import Callable

from .config import Config
from .detector import latest_turn
from .engine import EngineError
from .planner import Entry

ACTIVE_WRITER = "already has an active writer"


@dataclass
class Outcome:
    mode: str  # engine | desktop | pending-in-app | error
    ok: bool
    detail: str
    turn_status: str | None = None
    error_info: str | None = None


def queue_message(cfg: Config, thread_id: str, message: str, runner=subprocess.run) -> str:
    r = runner([cfg.codex_bin, "queue", "--thread", thread_id, "--message", message],
               capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "codex queue failed").strip()[:300])
    return (r.stdout or "").strip()


def _resume_params(cfg: Config, entry: Entry) -> dict:
    params: dict = {"threadId": entry.thread_id, "excludeTurns": True}
    if entry.model:
        params["model"] = entry.model
    if entry.effort:
        params["config"] = {"model_reasoning_effort": entry.effort}
    if cfg.approvals == "guardian":
        params["approvalsReviewer"] = "guardian_subagent"
    return params


def _turn_params(cfg: Config, entry: Entry) -> dict:
    params: dict = {"threadId": entry.thread_id, "input": [{"type": "text", "text": entry.message}]}
    if entry.model:
        params["model"] = entry.model
    if entry.effort:
        params["effort"] = entry.effort
    if cfg.approvals == "guardian":
        params["approvalsReviewer"] = "guardian_subagent"
    return params


def _outcome_from_turn(mode: str, turn: dict) -> Outcome:
    status = turn.get("status")
    err = turn.get("error") or {}
    detail = err.get("message") or status or "unknown"
    return Outcome(mode, status == "completed", str(detail)[:300], status, err.get("codexErrorInfo"))


def _deliver_via_desktop(cfg: Config, entry: Entry, log, sleep, now, latest, queue) -> Outcome:
    before = latest(cfg.codex_home, entry.thread_id)
    queue(cfg, entry.thread_id, entry.message)
    log(f"queued {entry.message!r} for the desktop engine (thread {entry.thread_id[:13]})")
    deadline = now() + cfg.desktop_wait_seconds
    while now() < deadline:
        sleep(5)
        cur = latest(cfg.codex_home, entry.thread_id)
        if cur and (before is None or cur[0] != before[0]):
            log(f"desktop started turn {cur[0][:13]}")
            end = now() + cfg.turn_timeout_seconds
            while cur[1] == "inProgress" and now() < end:
                sleep(15)
                cur = latest(cfg.codex_home, entry.thread_id) or cur
            status, info = cur[1], cur[4]
            return Outcome("desktop", status == "completed", info or status or "", status, info)
    return Outcome("pending-in-app", False, "queued; open the thread in the ChatGPT app to start it")


def deliver(cfg: Config, engine, entry: Entry, begin_watch: Callable[[str], object], log: Callable[[str], None],
            sleep=time.sleep, now=time.time, latest=latest_turn, queue=queue_message) -> Outcome:
    try:
        engine.call("thread/resume", _resume_params(cfg, entry), timeout=180)
    except EngineError as e:
        if ACTIVE_WRITER in e.message:
            return _deliver_via_desktop(cfg, entry, log, sleep, now, latest, queue)
        return Outcome("error", False, f"thread/resume failed: {e.message}")
    try:
        watch = begin_watch(entry.thread_id)
        res = engine.call("turn/start", _turn_params(cfg, entry), timeout=180) or {}
        turn_id = (res.get("turn") or {}).get("id", "?")
        log(f"turn {str(turn_id)[:13]} started in our engine for thread {entry.thread_id[:13]}")
        turn = watch.wait(cfg.turn_timeout_seconds)
        if turn is None:
            return Outcome("engine", False, "timed out waiting for turn/completed")
        return _outcome_from_turn("engine", turn)
    except EngineError as e:
        return Outcome("error", False, f"turn/start failed: {e.message}")
    finally:
        try:
            engine.call("thread/unsubscribe", {"threadId": entry.thread_id}, timeout=30)
        except Exception:  # noqa: BLE001
            pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_deliver -v`
Expected: 9 tests PASS

- [ ] **Step 5: Commit**

```bash
git add codex_resume/deliver.py tests/test_deliver.py
git commit -s -m "Add delivery via own engine or desktop queue"
```

---

### Task 9: Scheduler

**Files:**
- Create: `codex_resume/scheduler.py`, `tests/test_scheduler.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `Scheduler(cfg, state, notifier=notify, engine_factory=None, clock=time.time, finder=find_blocked, deliverer=deliver)` with `tick()`, `run_forever()`, `snapshot() -> dict`, `set_skip(tid, flag)`, `set_message(tid, message, model, effort)`, `resume_now(tid)`, `begin_watch(tid) -> TurnWatch`, `stop()`.
- `snapshot()` keys: `now, engine, limits, note, entries, running, history, config {port, weekly_guard, max_attempts, default_message, approvals}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_scheduler.py
import tempfile, threading, unittest
from pathlib import Path
from codex_resume.config import Config
from codex_resume.deliver import Outcome
from codex_resume.detector import Blocked
from codex_resume.scheduler import Scheduler
from codex_resume.state import State

NOW = 2_000_000
RL_BLOCKED = {"ordinaryUsageAllowed": False, "rateLimits": {"primary": {"usedPercent": 100, "resetsAt": NOW + 100, "windowDurationMins": 300}, "secondary": {"usedPercent": 20, "resetsAt": NOW + 9999, "windowDurationMins": 10080}}}
RL_OPEN = {"ordinaryUsageAllowed": True, "rateLimits": {"primary": {"usedPercent": 1, "resetsAt": NOW + 18000, "windowDurationMins": 300}, "secondary": {"usedPercent": 20, "resetsAt": NOW + 9999, "windowDurationMins": 10080}}}


class FakeEngine:
    def __init__(self, limits):
        self.limits = limits; self.started = 0; self.stopped = 0; self.on_notification = None
    def start(self): self.started += 1
    def stop(self): self.stopped += 1
    def alive(self): return True
    def call(self, method, params=None, timeout=60):
        if method == "account/rateLimits/read":
            return self.limits
        return {}


def blocked(tid, at):
    return Blocked(tid, "/p/x", "title", "last", at, "try again at 1:56 PM.", "gpt-6-astra", "xhigh", "vscode", tid + "-t")


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.tmp.name) / "s.json")
        self.cfg = Config(codex_bin="/bin/echo", codex_home=self.tmp.name, tick_seconds=1)
        self.clock = [float(NOW)]
        self.notes = []
        self.blocked = [blocked("a", 10), blocked("b", 20)]
        self.delivered = []
        self.done = threading.Event()

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, engine, outcome=Outcome("engine", True, "completed", "completed", None)):
        def deliverer(cfg, eng, entry, begin_watch, log, **kw):
            self.delivered.append(entry.thread_id); self.done.set(); return outcome
        self.engine = engine
        def factory(on_notification, on_server_request):
            engine.on_notification = on_notification; return engine
        return Scheduler(self.cfg, self.state, notifier=lambda t, x: self.notes.append(x), engine_factory=factory,
                         clock=lambda: self.clock[0], finder=lambda home: self.blocked, deliverer=deliverer)

    def test_tick_blocked_schedules_and_does_not_deliver(self):
        s = self.make(FakeEngine(RL_BLOCKED))
        s.tick()
        snap = s.snapshot()
        self.assertEqual(snap["engine"], "ok")
        self.assertEqual([e["thread_id"] for e in snap["entries"]], ["a", "b"])
        self.assertEqual(snap["entries"][0]["resume_at"], NOW + 100 + 60)
        self.assertEqual(self.delivered, [])
        self.assertEqual(snap["limits"]["primary"]["used_percent"], 100)

    def test_tick_open_delivers_one_then_next(self):
        s = self.make(FakeEngine(RL_OPEN))
        s.tick(); self.assertTrue(self.done.wait(2)); self.done.clear()
        s.join_delivery(2)
        self.assertEqual(self.delivered, ["a"])
        self.assertEqual(self.state.attempts_since("a", 0), 1)
        self.assertTrue(any("a" in h["thread_id"] for h in self.state.history()))
        s.tick(); self.assertTrue(self.done.wait(2)); s.join_delivery(2)
        self.assertEqual(self.delivered, ["a", "b"])
        self.assertGreaterEqual(self.engine.stopped, 1)  # engine restarted after an engine-mode delivery

    def test_pending_in_app_marks_state(self):
        s = self.make(FakeEngine(RL_OPEN), outcome=Outcome("pending-in-app", False, "queued"))
        s.tick(); self.assertTrue(self.done.wait(2)); s.join_delivery(2)
        self.assertIsNotNone(self.state.thread("a")["pending_in_app_at"])
        self.assertEqual(s.snapshot()["entries"][0]["state"], "pending-in-app")

    def test_engine_failure_marks_down_and_keeps_plan_from_hints(self):
        class Dead(FakeEngine):
            def call(self, method, params=None, timeout=60): raise RuntimeError("gone")
        s = self.make(Dead(RL_OPEN))
        s.tick()
        snap = s.snapshot()
        self.assertTrue(snap["engine"].startswith("down"))
        self.assertEqual(len(snap["entries"]), 2)
        self.assertEqual(self.delivered, [])

    def test_ui_actions(self):
        s = self.make(FakeEngine(RL_BLOCKED))
        s.tick()
        s.set_skip("a", True); s.set_message("b", "carry on", "m", "low")
        snap = s.snapshot()
        by = {e["thread_id"]: e for e in snap["entries"]}
        self.assertEqual(by["a"]["state"], "skipped")
        self.assertEqual((by["b"]["message"], by["b"]["model"], by["b"]["effort"]), ("carry on", "m", "low"))
        s.resume_now("b"); self.assertTrue(self.done.wait(2)); s.join_delivery(2)
        self.assertEqual(self.delivered, ["b"])

    def test_rate_limit_notification_updates_limits(self):
        s = self.make(FakeEngine(RL_BLOCKED))
        s.tick()
        self.engine.on_notification("account/rateLimits/updated", {"rateLimits": {"primary": {"usedPercent": 3, "resetsAt": 1}, "rateLimitReachedType": None}})
        self.assertEqual(s.snapshot()["limits"]["primary"]["used_percent"], 3)
        self.assertTrue(s.snapshot()["limits"]["allowed"])

    def test_turn_watch_receives_completion(self):
        s = self.make(FakeEngine(RL_BLOCKED))
        s.tick()
        w = s.begin_watch("a")
        self.engine.on_notification("turn/completed", {"threadId": "a", "turn": {"id": "x", "status": "completed"}})
        self.assertEqual(w.wait(1)["status"], "completed")
        self.assertIsNone(s.begin_watch("zzz").wait(0.01))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_scheduler -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'codex_resume.scheduler'`

- [ ] **Step 3: Write the implementation**

```python
# codex_resume/scheduler.py
from __future__ import annotations
import threading
import time
import traceback

from .config import Config
from .deliver import deliver, Outcome
from .detector import find_blocked
from .engine import Engine
from .limits import Limits, parse_limits, read_limits
from .notify import notify
from .planner import Plan, build_plan
from .state import State

LIMITS_EVERY = 300
LIMITS_NEAR_RESET = 55
NEAR_RESET_WINDOW = 900


class TurnWatch:
    def __init__(self):
        self._ev = threading.Event()
        self.turn: dict | None = None

    def set(self, turn: dict) -> None:
        self.turn = turn
        self._ev.set()

    def wait(self, timeout: float | None) -> dict | None:
        self._ev.wait(timeout)
        return self.turn


class Scheduler:
    def __init__(self, cfg: Config, state: State, notifier=notify, engine_factory=None, clock=time.time,
                 finder=find_blocked, deliverer=deliver):
        self.cfg, self.state, self.notify, self.clock = cfg, state, notifier, clock
        self.finder, self.deliverer = finder, deliverer
        self.engine_factory = engine_factory or (lambda on_notification, on_server_request: Engine(
            cfg.codex_bin, on_notification=on_notification, on_server_request=on_server_request))
        self._engine = None
        self.engine_status = "down: not started"
        self.limits: Limits | None = None
        self.limits_at = 0
        self.blocked = []
        self.plan = Plan([], None, "")
        self.running: str | None = None
        self._worker: threading.Thread | None = None
        self._watches: dict[str, TurnWatch] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self.last_tick = 0
        self.log_lines: list[str] = []

    # ---- logging / engine -------------------------------------------------
    def log(self, text: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {text}"
        print(line, flush=True)
        self.log_lines = (self.log_lines + [line])[-200:]

    def engine(self):
        with self._lock:
            if self._engine is None or not self._engine.alive():
                self._engine = self.engine_factory(self._on_notification, self._on_server_request)
                self._engine.start()
            return self._engine

    def _reset_engine(self) -> None:
        with self._lock:
            if self._engine is not None:
                try:
                    self._engine.stop()
                except Exception:  # noqa: BLE001
                    pass
                self._engine = None

    def _on_notification(self, method: str, params: dict) -> None:
        if method == "turn/completed":
            w = self._watches.get(params.get("threadId", ""))
            if w:
                w.set(params.get("turn") or {})
        elif method == "account/rateLimits/updated":
            self.limits = parse_limits({"rateLimits": params.get("rateLimits") or {}}, int(self.clock()))
            self.limits_at = int(self.clock())
        elif method == "error":
            err = params.get("error") or {}
            self.log(f"engine error on thread {str(params.get('threadId'))[:13]}: {err.get('message', '')[:120]}")

    def _on_server_request(self, method: str, params: dict):
        if "requestApproval" in method:
            tid = str(params.get("threadId", ""))[:13]
            self.log(f"approval requested in thread {tid} ({method}); declined")
            self.state.add_history(int(self.clock()), params.get("threadId"), "approval-declined", method)
            self.notify("codex-resume", f"Thread {tid} asked for approval; declined. Open it in the app.")
        return Engine.default_server_request(method, params)

    def begin_watch(self, thread_id: str) -> TurnWatch:
        w = TurnWatch()
        self._watches[thread_id] = w
        return w

    # ---- tick -------------------------------------------------------------
    def _limits_due(self, now: int) -> bool:
        if now - self.limits_at >= LIMITS_EVERY:
            return True
        if self.limits and self.limits.primary and self.limits.primary.resets_at:
            until = self.limits.primary.resets_at + self.cfg.grace_seconds - now
            if -NEAR_RESET_WINDOW <= until <= NEAR_RESET_WINDOW and now - self.limits_at >= LIMITS_NEAR_RESET:
                return True
        return False

    def tick(self) -> None:
        now = int(self.clock())
        if self._limits_due(now):
            try:
                self.limits = read_limits(self.engine(), now)
                self.limits_at = now
                self.engine_status = "ok"
            except Exception as e:  # noqa: BLE001
                self.engine_status = f"down: {str(e)[:120]}"
                self.log(self.engine_status)
                self._reset_engine()
        try:
            self.blocked = self.finder(self.cfg.codex_home)
        except Exception as e:  # noqa: BLE001
            self.log(f"detector failed: {e}")
        blocked_ids = {b.thread_id for b in self.blocked}
        for tid, t in list(self.state.data["threads"].items()):
            if t.get("pending_in_app_at") and tid not in blocked_ids:
                self.state.set_pending_in_app(tid, None)
        with self._lock:
            self.plan = build_plan(self.blocked, self.limits, self.state, self.cfg, now, self.running)
            nxt = self.plan.next_action
            if nxt and self.running is None:
                self._start_delivery(nxt)
        self.last_tick = now

    def _start_delivery(self, entry) -> None:
        now = int(self.clock())
        self.running = entry.thread_id
        self.state.record_attempt(entry.thread_id, now)
        self.state.set_resume_now(entry.thread_id, False)
        self.state.add_history(now, entry.thread_id, "resuming", f"{entry.message!r} ({entry.title[:60]})")
        self.log(f"resuming thread {entry.thread_id[:13]} with {entry.message!r}")
        self.notify("codex-resume", f"Resuming: {entry.title[:80]}")
        self._worker = threading.Thread(target=self._deliver, args=(entry,), daemon=True, name="codex-resume-deliver")
        self._worker.start()

    def _deliver(self, entry) -> None:
        outcome: Outcome
        try:
            outcome = self.deliverer(self.cfg, self.engine(), entry, self.begin_watch, self.log)
        except Exception as e:  # noqa: BLE001
            outcome = Outcome("error", False, f"{type(e).__name__}: {e}")
            traceback.print_exc()
        now = int(self.clock())
        self._watches.pop(entry.thread_id, None)
        with self._lock:
            self.running = None
        if outcome.mode == "pending-in-app":
            self.state.set_pending_in_app(entry.thread_id, now)
        summary = f"{outcome.mode}: {outcome.turn_status or ''} {outcome.detail}".strip()
        self.state.set_last_result(entry.thread_id, summary)
        self.state.add_history(now, entry.thread_id, "done" if outcome.ok else "ended", summary)
        self.log(f"thread {entry.thread_id[:13]} {summary}")
        self.notify("codex-resume", f"{'Finished' if outcome.ok else 'Stopped'}: {entry.title[:60]} ({outcome.mode})")
        if outcome.mode in ("engine", "error"):
            self._reset_engine()  # releases the thread writer lock for the desktop
        self.limits_at = 0

    def join_delivery(self, timeout: float) -> None:
        if self._worker:
            self._worker.join(timeout)

    # ---- UI actions ---------------------------------------------------------
    def set_skip(self, tid: str, flag: bool) -> None:
        self.state.set_skip(tid, flag)
        self.state.add_history(int(self.clock()), tid, "skip" if flag else "unskip")
        self._replan()

    def set_message(self, tid: str, message: str | None, model: str | None = None, effort: str | None = None) -> None:
        self.state.set_message(tid, message, model, effort)
        self._replan()

    def resume_now(self, tid: str) -> None:
        self.state.set_resume_now(tid, True)
        self.state.set_pending_in_app(tid, None)
        self.state.set_skip(tid, False)
        self.state.add_history(int(self.clock()), tid, "resume-now")
        self.tick()

    def _replan(self) -> None:
        with self._lock:
            self.plan = build_plan(self.blocked, self.limits, self.state, self.cfg, int(self.clock()), self.running)

    # ---- snapshot / loop ----------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            entries = [e.to_dict() for e in self.plan.entries]
            for e in entries:
                e["last_result"] = self.state.thread(e["thread_id"]).get("last_result")
            return {
                "now": int(self.clock()),
                "engine": self.engine_status,
                "limits": self.limits.to_dict() if self.limits else None,
                "note": self.plan.note,
                "entries": entries,
                "running": self.running,
                "history": self.state.history(50),
                "log": self.log_lines[-30:],
                "config": {"port": self.cfg.port, "weekly_guard": self.cfg.weekly_guard, "max_attempts": self.cfg.max_attempts,
                           "default_message": self.cfg.default_message, "approvals": self.cfg.approvals},
            }

    def run_forever(self) -> None:
        self.log(f"codex-resume started; engine {self.cfg.codex_bin}")
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001
                self.log(f"tick failed: {e}")
                traceback.print_exc()
            self._stop.wait(self.cfg.tick_seconds)

    def stop(self) -> None:
        self._stop.set()
        self._reset_engine()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_scheduler -v`
Expected: 7 tests PASS

- [ ] **Step 5: Commit**

```bash
git add codex_resume/scheduler.py tests/test_scheduler.py
git commit -s -m "Add scheduler tick loop and delivery worker"
```

---

### Task 10: Web page and API

**Files:**
- Create: `codex_resume/web.py`, `tests/test_web.py`

**Interfaces:**
- Consumes: `Scheduler.snapshot/set_skip/set_message/resume_now` (Task 9).
- Produces: `make_server(scheduler, host="127.0.0.1", port=8765) -> ThreadingHTTPServer`. Routes: `GET /` HTML, `GET /api/state` JSON snapshot, `POST /api/threads/<id>/skip`, `/unskip`, `/resume-now`, `/message` (JSON body `{"message": str, "model": str|null, "effort": str|null}`). Every POST returns `{"ok": true}`; unknown routes return 404 JSON.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_web.py
import http.client, json, threading, unittest
from codex_resume.web import make_server


class FakeScheduler:
    def __init__(self):
        self.calls = []
    def snapshot(self):
        return {"now": 1, "engine": "ok", "limits": None, "note": "", "entries": [], "running": None, "history": [], "log": [], "config": {}}
    def set_skip(self, tid, flag): self.calls.append(("skip", tid, flag))
    def set_message(self, tid, message, model=None, effort=None): self.calls.append(("message", tid, message, model, effort))
    def resume_now(self, tid): self.calls.append(("now", tid))


class WebTest(unittest.TestCase):
    def setUp(self):
        self.sched = FakeScheduler()
        self.server = make_server(self.sched, port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close()

    def req(self, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers={"Content-Type": "application/json"})
        r = c.getresponse(); data = r.read(); c.close()
        return r.status, r.getheader("Content-Type", ""), data

    def test_page_and_state(self):
        st, ct, data = self.req("GET", "/")
        self.assertEqual(st, 200); self.assertIn("text/html", ct); self.assertIn(b"codex-resume", data)
        st, ct, data = self.req("GET", "/api/state")
        self.assertEqual(st, 200); self.assertEqual(json.loads(data)["engine"], "ok")

    def test_actions(self):
        self.assertEqual(self.req("POST", "/api/threads/t1/skip")[0], 200)
        self.assertEqual(self.req("POST", "/api/threads/t1/unskip")[0], 200)
        self.assertEqual(self.req("POST", "/api/threads/t1/resume-now")[0], 200)
        self.assertEqual(self.sched.calls, [("skip", "t1", True), ("skip", "t1", False), ("now", "t1")])

    def test_post_message_roundtrip(self):
        msg = 'say "hi" — ünï'
        st, _, data = self.req("POST", "/api/threads/t1/message", {"message": msg, "model": "gpt-6-astra", "effort": "xhigh"})
        self.assertEqual(st, 200); self.assertTrue(json.loads(data)["ok"])
        self.assertEqual(self.sched.calls[-1], ("message", "t1", msg, "gpt-6-astra", "xhigh"))

    def test_unknown_routes(self):
        self.assertEqual(self.req("GET", "/nope")[0], 404)
        self.assertEqual(self.req("POST", "/api/threads/t1/explode")[0], 404)
        self.assertEqual(self.req("POST", "/api/threads/t1/message", None)[0], 400)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_web -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'codex_resume.web'`

- [ ] **Step 3: Write the implementation**

```python
# codex_resume/web.py
from __future__ import annotations
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>codex-resume</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{color-scheme:light dark;--fg:#1d1d1f;--bg:#f5f5f7;--card:#fff;--muted:#6e6e73;--bar:#e5e5ea;--ok:#34c759;--warn:#ff9f0a;--bad:#ff3b30;--accent:#0a84ff}
@media(prefers-color-scheme:dark){:root{--fg:#f5f5f7;--bg:#1c1c1e;--card:#2c2c2e;--muted:#98989d;--bar:#3a3a3c}}
body{margin:0;padding:24px;font:14px/1.45 -apple-system,BlinkMacSystemFont,"SF Pro Text",Helvetica,Arial,sans-serif;background:var(--bg);color:var(--fg)}
h1{font-size:20px;margin:0 0 4px}h2{font-size:14px;margin:24px 0 8px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.sub{color:var(--muted);margin-bottom:16px}.card{background:var(--card);border-radius:12px;padding:16px;box-shadow:0 1px 2px rgba(0,0,0,.08)}
.gauge{display:grid;grid-template-columns:90px 1fr 260px;gap:12px;align-items:center;margin:8px 0}
.bar{height:10px;background:var(--bar);border-radius:5px;overflow:hidden}.bar i{display:block;height:100%;background:var(--ok)}
.bar.warn i{background:var(--warn)}.bar.bad i{background:var(--bad)}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:8px 6px;border-bottom:1px solid var(--bar);vertical-align:top}
th{color:var(--muted);font-weight:500}.ask{max-width:380px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.state{display:inline-block;padding:2px 8px;border-radius:10px;font-size:12px;background:var(--bar)}
.state.scheduled{background:#d1e9ff;color:#0a4a8a}.state.running{background:#d6f5de;color:#0b6b2a}.state.skipped,.state.max-attempts,.state.weekly-guard{opacity:.7}
.state.pending-in-app{background:#ffe8c2;color:#7a4a00}
button{font:inherit;font-size:12px;padding:4px 10px;border-radius:8px;border:1px solid var(--bar);background:var(--card);color:var(--fg);cursor:pointer;margin-right:4px}
button.primary{background:var(--accent);color:#fff;border-color:var(--accent)}
.log{font:12px/1.5 ui-monospace,Menlo,monospace;white-space:pre-wrap;color:var(--muted);max-height:260px;overflow:auto}
.empty{color:var(--muted);padding:12px 0}
</style></head><body>
<h1>codex-resume</h1><div class="sub" id="sub">loading…</div>
<div class="card" id="limits"></div>
<h2>Blocked threads</h2><div class="card" id="threads"></div>
<h2>History</h2><div class="card log" id="history"></div>
<h2>Log</h2><div class="card log" id="log"></div>
<script>
const fmtT=s=>s?new Date(s*1000).toLocaleString([], {weekday:'short',hour:'2-digit',minute:'2-digit'}):'';
const rel=(s,now)=>{if(!s)return'';const d=s-now;const a=Math.abs(d);const m=Math.round(a/60);const t=m<60?m+' min':(Math.round(a/360)/10)+' h';return d>=0?'in '+t:t+' ago'};
const esc=t=>(t||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function post(p,b){await fetch(p,{method:'POST',headers:{'Content-Type':'application/json'},body:b?JSON.stringify(b):null});load()}
function gauge(name,w,now){if(!w)return`<div class="gauge"><b>${name}</b><div class="bar"><i style="width:0"></i></div><span>unknown</span></div>`;
 const c=w.used_percent>=100?'bad':(w.used_percent>=80?'warn':'');return`<div class="gauge"><b>${name}</b><div class="bar ${c}"><i style="width:${Math.min(100,w.used_percent)}%"></i></div><span>${w.used_percent}% used · resets ${fmtT(w.resets_at)} (${rel(w.resets_at,now)})</span></div>`}
function row(e,now){const when=e.state==='scheduled'?(e.position===1?fmtT(e.resume_at)+' <small>('+rel(e.resume_at,now)+')</small>':'after #'+(e.position-1)):(e.state==='running'?'running now':'');
 return`<tr><td><b>${esc(e.cwd.split('/').pop())}</b><div class="ask" title="${esc(e.title)}">${esc(e.title)}</div></td>
 <td class="ask" title="${esc(e.last_user_message)}">${esc(e.last_user_message)}</td><td>${fmtT(e.blocked_at)}</td><td>${when}</td>
 <td><span class="state ${e.state}">${e.state}${e.position&&e.state==='scheduled'?' #'+e.position:''}</span><div class="ask" title="${esc(e.reason)}">${esc(e.reason)}</div>${e.last_result?`<div class="ask" title="${esc(e.last_result)}">last: ${esc(e.last_result)}</div>`:''}</td>
 <td><code>${esc(e.message)}</code><div class="ask">${esc(e.model||'')} ${esc(e.effort||'')}</div></td>
 <td style="white-space:nowrap">${e.state==='skipped'?`<button onclick="post('/api/threads/${e.thread_id}/unskip')">Unskip</button>`:`<button onclick="post('/api/threads/${e.thread_id}/skip')">Skip</button>`}
 <button class="primary" onclick="post('/api/threads/${e.thread_id}/resume-now')">Now</button>
 <button onclick="editMsg('${e.thread_id}',${JSON.stringify(e.message)},${JSON.stringify(e.model||'')},${JSON.stringify(e.effort||'')})">Edit</button></td></tr>`}
function editMsg(id,msg,model,effort){const m=prompt('Message to send when resuming:',msg);if(m===null)return;
 const me=prompt('Model/effort (blank keeps thread defaults), e.g. gpt-6-astra/xhigh:',model?model+'/'+(effort||''):'');if(me===null)return;
 const [mo,ef]=me.split('/');post('/api/threads/'+id+'/message',{message:m,model:mo||null,effort:ef||null})}
async function load(){try{const s=await(await fetch('/api/state')).json();const now=s.now;
 document.getElementById('sub').textContent=`engine: ${s.engine} · updated ${new Date().toLocaleTimeString()} · ${s.config.approvals} approvals · weekly guard ${s.config.weekly_guard}% · max ${s.config.max_attempts}/day${s.note?' · '+s.note:''}`;
 const L=s.limits||{};document.getElementById('limits').innerHTML=gauge('5h window',L.primary,now)+gauge('weekly',L.secondary,now)+`<div class="sub" style="margin:6px 0 0">usage allowed: ${L.allowed===true?'yes':(L.allowed===false?'no':'unknown')} · plan ${L.plan||'?'} · read ${fmtT(L.fetched_at)}</div>`;
 document.getElementById('threads').innerHTML=s.entries.length?`<table><tr><th>Project / thread</th><th>Last ask</th><th>Blocked</th><th>Resume at</th><th>State</th><th>Message</th><th></th></tr>${s.entries.map(e=>row(e,now)).join('')}</table>`:'<div class="empty">No threads are blocked on the usage limit.</div>';
 document.getElementById('history').textContent=s.history.map(h=>`${fmtT(h.ts)}  ${(h.thread_id||'').slice(0,13).padEnd(13)}  ${h.event.padEnd(18)} ${h.detail||''}`).join('\n')||'nothing yet';
 document.getElementById('log').textContent=(s.log||[]).join('\n');}catch(e){document.getElementById('sub').textContent='daemon unreachable: '+e}}
load();setInterval(load,10000);
</script></body></html>"""


def make_server(scheduler, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "codex-resume"

        def log_message(self, fmt, *args):  # quiet
            pass

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, obj) -> None:
            self._send(status, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self):
            if self.path == "/" or self.path.startswith("/?"):
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif self.path == "/api/state":
                self._json(200, scheduler.snapshot())
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            parts = self.path.strip("/").split("/")
            if len(parts) != 4 or parts[:2] != ["api", "threads"]:
                return self._json(404, {"error": "not found"})
            tid, action = parts[2], parts[3]
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if action == "skip":
                scheduler.set_skip(tid, True)
            elif action == "unskip":
                scheduler.set_skip(tid, False)
            elif action == "resume-now":
                scheduler.resume_now(tid)
            elif action == "message":
                try:
                    body = json.loads(raw.decode("utf-8")) if raw else None
                except (json.JSONDecodeError, UnicodeDecodeError):
                    body = None
                if not isinstance(body, dict) or not isinstance(body.get("message"), str) or not body["message"].strip():
                    return self._json(400, {"error": "message required"})
                scheduler.set_message(tid, body["message"], body.get("model") or None, body.get("effort") or None)
            else:
                return self._json(404, {"error": "unknown action"})
            self._json(200, {"ok": True})

    return ThreadingHTTPServer((host, port), Handler)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_web -v`
Expected: 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add codex_resume/web.py tests/test_web.py
git commit -s -m "Add local web page and API"
```

---

### Task 11: CLI entry point, launchd install, README

**Files:**
- Create: `codex_resume/__main__.py`, `README.md`, `tests/test_cli.py`

**Interfaces:**
- Produces: `python3 -m codex_resume run|status|install-launchd|uninstall-launchd`; `build_plist(python: str, workdir: str, data_dir: str) -> str`; `format_status(snapshot: dict) -> str`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli.py
import unittest
from codex_resume.__main__ import build_plist, format_status

class CliTest(unittest.TestCase):
    def test_plist_contents(self):
        p = build_plist("/usr/bin/python3", "/work", "/data")
        self.assertIn("<string>/usr/bin/python3</string>", p)
        self.assertIn("<string>codex_resume</string>", p)
        self.assertIn("<key>WorkingDirectory</key>\n    <string>/work</string>", p)
        self.assertIn("/data/daemon.log", p)
        self.assertIn("<key>KeepAlive</key>", p)

    def test_format_status(self):
        snap = {"now": 100, "engine": "ok", "note": "", "limits": {"primary": {"used_percent": 100, "resets_at": 160, "window_minutes": 300}, "secondary": {"used_percent": 20, "resets_at": 900, "window_minutes": 10080}, "allowed": False, "plan": "plus", "fetched_at": 100},
                "entries": [{"thread_id": "01a0fe2e-7ce2-abc", "cwd": "/p/cleverplayhouse", "title": "T", "last_user_message": "make it so", "blocked_at": 50, "resume_at": 220, "state": "scheduled", "position": 1, "message": "continue", "model": "m", "effort": "e", "attempts": 0, "reason": "after reset"}],
                "running": None, "history": [], "log": [], "config": {}}
        out = format_status(snap)
        self.assertIn("5h window 100%", out)
        self.assertIn("cleverplayhouse", out)
        self.assertIn("scheduled #1", out)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_cli -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Write the implementation**

```python
# codex_resume/__main__.py
from __future__ import annotations
import argparse
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from .config import Config
from .scheduler import Scheduler
from .state import State
from .web import make_server

LABEL = "com.codex-resume"


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


def build_plist(python: str, workdir: str, data_dir: str) -> str:
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
        <string>codex_resume</string>
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
        <string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>
</dict>
</plist>
"""


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def cmd_run(cfg: Config) -> None:
    Path(cfg.data_dir).mkdir(parents=True, exist_ok=True)
    sched = Scheduler(cfg, State(Path(cfg.data_dir) / "state.json"))
    server = make_server(sched, port=cfg.port)
    threading.Thread(target=server.serve_forever, daemon=True, name="codex-resume-web").start()
    sched.log(f"web UI on http://127.0.0.1:{cfg.port}")
    try:
        sched.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        sched.stop()
        server.shutdown()


def cmd_status(cfg: Config) -> None:
    sched = Scheduler(cfg, State(Path(cfg.data_dir) / "state.json"), notifier=lambda t, x: None)
    sched.tick()
    print(format_status(sched.snapshot()))
    sched.stop()


def cmd_install(cfg: Config) -> None:
    Path(cfg.data_dir).mkdir(parents=True, exist_ok=True)
    p = _plist_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(build_plist(sys.executable, str(Path(__file__).resolve().parent.parent), cfg.data_dir))
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True)
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
    ap = argparse.ArgumentParser(prog="codex-resume")
    ap.add_argument("command", choices=["run", "status", "install-launchd", "uninstall-launchd"])
    ap.add_argument("--config", default=None)
    args = ap.parse_args(argv)
    cfg = Config.load(args.config)
    {"run": cmd_run, "status": cmd_status, "install-launchd": cmd_install, "uninstall-launchd": cmd_uninstall}[args.command](cfg)


if __name__ == "__main__":
    main()
```

```markdown
# codex-resume

Continues Codex threads in the ChatGPT desktop app after a usage-limit reset.

## Run

    python3 -m codex_resume status            # one-shot table
    python3 -m codex_resume run               # daemon + web UI on http://127.0.0.1:8765
    python3 -m codex_resume install-launchd   # start at login (launchd)
    python3 -m codex_resume uninstall-launchd

## How it works

Every 60s it reads your rate limits through its own `codex app-server` engine,
finds threads whose last turn failed with `usageLimitExceeded` (read-only from
`~/.codex/state_5.sqlite` and `thread_history_1.sqlite`), and once the 5h
window is open it resumes them one at a time: `thread/resume` + `turn/start`
in its own engine, or `codex queue` when the desktop app has the thread open.

## Config

Optional `~/.codex-resume/config.json`:

    {"port": 8765, "weekly_guard": 90, "max_attempts": 3, "approvals": "guardian",
     "default_message": "continue", "grace_seconds": 60}

`approvals`: `guardian` routes approval prompts through Codex's guardian
reviewer (what the desktop app does); `decline` declines them and notifies you.

## Tests

    python3 -m unittest discover -s tests -v
```

- [ ] **Step 4: Run tests and the real status command**

Run: `python3 -m unittest discover -s tests -v`
Expected: all PASS

Run: `python3 -m codex_resume status`
Expected: engine ok, the 5h/weekly gauges, and the blocked cleverplayhouse threads listed as `scheduled #1` / `scheduled #2` with the reset time.

- [ ] **Step 5: Commit**

```bash
git add codex_resume/__main__.py README.md tests/test_cli.py
git commit -s -m "Add CLI, launchd install and README"
```

---

### Task 12: Live smoke and launchd install

**Files:** none new.

- [ ] **Step 1: Start the daemon in the foreground for 90 seconds**

Run: `python3 -m codex_resume run & sleep 90; curl -s http://127.0.0.1:8765/api/state | python3 -m json.tool | head -60; kill %1`
Expected: `engine` is `ok`, `limits.primary.used_percent` matches `codex` usage, two entries scheduled, no deliveries while `allowed` is false.

- [ ] **Step 2: Open the page**

Run: `open http://127.0.0.1:8765`
Expected: gauges and the queue table render; Skip / Now / Edit buttons call the API.

- [ ] **Step 3: Install under launchd and verify**

Run: `python3 -m codex_resume install-launchd && sleep 5 && launchctl print gui/$(id -u)/com.codex-resume | grep -E 'state|pid' && tail -5 ~/.codex-resume/daemon.log`
Expected: state `running`, log shows "web UI on http://127.0.0.1:8765".

- [ ] **Step 4: Canary at the next reset**

At the reset time plus one minute, check `~/.codex-resume/daemon.log` and the page: thread 1 goes `running`, turns appear in the ChatGPT app for that thread (the desktop reloads history from the shared store), thread 2 starts when thread 1 completes. Record the outcome in the README under a "Verified" line.
