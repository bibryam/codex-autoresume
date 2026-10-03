# codex-resume

Continues Codex threads in the ChatGPT desktop app after a usage-limit reset,
so you do not have to come back and type "continue" yourself.

## Run it

Background (recommended). Installs a launchd agent that starts at login and
restarts if it crashes, then opens the page:

    ./start.sh

Stop and remove the background agent:

    ./stop.sh

Foreground, in a terminal (Ctrl-C to stop; useful for debugging):

    python3 -m codex_resume run

Either way the page is at http://127.0.0.1:8765 and the log is at
`~/.codex-resume/daemon.log`. `python3 -m codex_resume status` prints the
same information as a table and never changes anything.

## What you see on the page

- The 5h and weekly windows with their reset times.
- Every thread blocked on the usage limit: project, last ask, when it was
  blocked, when it will resume, its state, and the message it will be sent.
- Buttons per thread: **Skip** (leave it alone), **Now** (resume immediately,
  bypassing the guards), **Edit** (change the message, model and reasoning
  effort used for the resume).

States: `scheduled #n` (will resume in that order), `running`, `cooldown`
(resumed in the last 10 min), `waiting-reset` (failed again on the limit),
`weekly-guard`, `max-attempts`, `automation` (scheduled heartbeat thread,
never auto-resumed), `pending-in-app` (queued inside the app; starts when
the app picks it up or you open the thread).

## How it works

Every 60s it reads your rate limits through its own `codex app-server` engine
and finds threads whose last turn failed with `usageLimitExceeded` (read-only
from `~/.codex/state_5.sqlite` and `thread_history_1.sqlite`). Once the 5h
window is open it resumes them one at a time, oldest first:

- thread not open in the app: `thread/resume` + `turn/start` in its own engine
  (same binary, same `~/.codex`);
- thread open in the app: `codex queue` and wait for the app to run it.
  Verified on ChatGPT.app with Codex CLI 0.160.0: the app picks a queued
  message up within about 10 seconds and starts the turn itself.

A thread that fails again on the limit waits for the next window. Nothing is
sent while limits cannot be read.

## Config

Optional `~/.codex-resume/config.json`:

    {"port": 8765, "weekly_guard": 90, "max_attempts": 3, "approvals": "guardian",
     "default_message": "continue", "grace_seconds": 60, "retry_cooldown_seconds": 600}

`approvals`: `guardian` routes approval prompts through Codex's guardian
reviewer (what the desktop app does); `decline` declines them and notifies you.

## Tests

    python3 -m unittest discover -s tests -v
