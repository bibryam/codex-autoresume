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
     "default_message": "continue", "grace_seconds": 60, "retry_cooldown_seconds": 600}

`approvals`: `guardian` routes approval prompts through Codex's guardian
reviewer (what the desktop app does); `decline` declines them and notifies you.

## Tests

    python3 -m unittest discover -s tests -v
