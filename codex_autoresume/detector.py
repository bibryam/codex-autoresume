from __future__ import annotations
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, asdict
from pathlib import Path
from urllib.parse import quote

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
    is_automation: bool = False  # a scheduled heartbeat/automation posted into this thread recently

    def to_dict(self) -> dict:
        return asdict(self)


RECENT_USER_MESSAGES = 5


def _ro(path: Path):
    return closing(sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, timeout=5))


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
    try:
        with _ro(hist) as h:
            row = h.execute(
                "select turn_id, status, started_at, completed_at, error_json from thread_turns "
                "where thread_id=? order by rollout_ordinal desc limit 1", (thread_id,)).fetchone()
    except sqlite3.Error:
        return None
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
            items = h.execute(
                "select item_json from thread_items where thread_id=? and item_type='userMessage' "
                "order by rollout_ordinal desc limit ?", (tid, RECENT_USER_MESSAGES)).fetchall()
            texts = [_user_text(row[0]) for row in items]
            out.append(Blocked(
                thread_id=tid, cwd=cwd or "", title=(title or first_msg or "").strip()[:200],
                last_user_message=(texts[0] if texts else "")[:300],
                blocked_at=int(turn[4] or turn[3] or 0), reset_hint=message, model=model, effort=effort,
                source=source or "", last_turn_id=turn[0],
                is_automation=any(t.lstrip().startswith("<heartbeat>") for t in texts)))
    return out


def queued_count(codex_home: str, thread_id: str) -> int:
    """Messages waiting in Codex's durable queue (written by `codex queue`) for this thread."""
    qdb = Path(codex_home) / "queue_1.sqlite"
    if not qdb.exists():
        return 0
    try:
        with _ro(qdb) as q:
            row = q.execute("select count(*) from queued_items where thread_id=?", (thread_id,)).fetchone()
        return int(row[0]) if row else 0
    except sqlite3.Error:
        return 0
