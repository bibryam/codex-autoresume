# codex-autoresume

Continues Codex threads in the ChatGPT desktop app after a usage-limit reset,
so you do not have to come back and type "continue" yourself.

## Run it

Background (recommended):

    ./start.sh

This writes `~/Library/LaunchAgents/com.codex-autoresume.plist` and loads it with
`launchctl`. macOS then starts the daemon at every login (including after a
reboot) and restarts it if it crashes. That is the only change made to your
machine; nothing is installed system-wide and no root is needed. The script
also opens the page. Another user clones this folder and runs the same
command.

Stop the daemon and remove the launch agent again:

    ./stop.sh

Foreground, in a terminal (Ctrl-C to stop; useful for debugging):

    python3 -m codex_autoresume run

Either way the page is at http://127.0.0.1:8765 and the log is at
`~/.codex-autoresume/daemon.log`. `python3 -m codex_autoresume status` prints
the same information as a table; it asks the running daemon, or starts a
temporary engine of its own if none is running, and never resumes anything.

## What you see on the page

- The 5h and weekly windows with their reset times.
- Every thread blocked on the usage limit: project, last ask, when it was
  blocked, whether and when it will resume, and the message it will be sent.
- Buttons per thread:
  - **Skip**: do not resume this thread (red; disabled on rows that already
    will not resume).
  - **Schedule**: put a parked row back into the queue. It clears Skip and,
    for one resume, overrides the automation rule and the guards below.
  - **Now**: resume immediately, ignoring the window and the guards.
  - **Edit**: change the message, model and reasoning effort for the resume.
    The lists come from Codex; the thread's own values are preselected.

The "Will it resume?" column reads `Yes · #n` (in that order, with the time
for #1), `Yes · waiting` (10-minute cooldown after an attempt, or waiting for
the next window after a repeat failure), `Queued in the app` (the app will
start it, or open the thread), or `No` with the reason: skipped, automation
thread (a scheduled heartbeat posted into it), weekly guard, or the daily
attempt limit.

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

Optional `~/.codex-autoresume/config.json`:

    {"port": 8765, "weekly_guard": 90, "max_attempts": 3, "approvals": "guardian",
     "default_message": "continue", "grace_seconds": 60, "retry_cooldown_seconds": 600,
     "tick_seconds": 60, "desktop_wait_seconds": 90, "turn_timeout_seconds": 14400,
     "codex_bin": "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"}

- `weekly_guard`: no automatic resume once weekly usage reaches this percent.
- `max_attempts`: resumes per thread per 24 hours (automatic and Now).
- `approvals`: `guardian` routes approval prompts through Codex's guardian
  reviewer (what the desktop app does); `decline` declines them and notifies you.
- `grace_seconds`: wait after the reset time before resuming.
- `desktop_wait_seconds`: how long to wait for the app to pick up a queued message.
- `turn_timeout_seconds`: give up waiting for a resumed turn after this long.

## Tests

    python3 -m unittest discover -s tests -v
