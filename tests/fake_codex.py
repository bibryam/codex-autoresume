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
