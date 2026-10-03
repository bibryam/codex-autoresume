from __future__ import annotations
import itertools
import json
import subprocess
import threading
from typing import Callable

from . import __version__

Notification = Callable[[str, dict], None]
ServerRequest = Callable[[str, dict], "tuple[dict | None, dict | None]"]


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
    def default_server_request(method: str, params: dict) -> "tuple[dict | None, dict | None]":
        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            return {"decision": "decline"}, None
        return None, {"code": -32601, "message": f"{method} is not handled by codex-resume"}
