# codex-autoresume design

## Purpose

The ChatGPT desktop app runs Codex threads against a 5-hour usage window.
When the window is used up, the running turn fails with
`usageLimitExceeded` and the thread sits idle until someone types
"continue", often hours after the window reopened. codex-autoresume watches
for that state and continues the threads automatically, one at a time, as
soon as the window is open again.

## Architecture

```
 codex-autoresume (python3, stdlib only, launchd agent)
 ┌────────────────────────────────────────────────────────────────┐
 │ scheduler tick (60s)                                           │
 │   limits   <- own engine: account/rateLimits/read              │
 │   blocked  <- detector: ~/.codex sqlite, read-only             │
 │   plan     <- planner (pure): order, resume_at, state          │
 │   deliver  -> one thread at a time (worker thread)             │
 │   notify   -> macOS notification                               │
 │   state    -> ~/.codex-autoresume/state.json                   │
 ├────────────────────────────────────────────────────────────────┤
 │ web page   http://127.0.0.1:8765  (/api/state, per-thread POST)│
 └──────────────┬─────────────────────────────────────────────────┘
                │ JSON-RPC over stdio
       own engine: codex app-server --listen stdio://  ──► ~/.codex
                                                            ▲
       ChatGPT.app ── desktop engine (private pipe) ────────┘
```

Modules: `engine` (JSON-RPC client), `limits`, `detector`, `planner`,
`deliver`, `state`, `scheduler`, `web`, `notify`, `__main__` (CLI).

## Delivery

```
scheduler            own engine              desktop engine
   | thread/resume ─────►|
   |◄── ok               |          thread not open in the app
   | turn/start ────────►|   (or: the queued message starts the turn)
   |◄── turn/completed   |
   | thread/unsubscribe, engine restart (releases the writer lock)
   |
   |◄── "already has an active writer"     thread open in the app
   | codex queue --thread <id> --message "continue"
   |                                       ──► app drains the queue
   | poll thread_turns until the new turn completes
```

## Rules

- Blocked = last turn `failed` with `codexErrorInfo == usageLimitExceeded`
  and no later turn. Subagent threads are ignored.
- `resume_at = resets_at + grace`; oldest blocked first; the next thread
  starts when the previous turn ends.
- Delivery needs fresh limits that say usage is allowed, or a reset time
  that has demonstrably passed. Stale or unreadable limits never deliver.
- Guards: weekly usage above `weekly_guard`, more than `max_attempts`
  automatic resumes in 24h, a 10-minute cooldown after any attempt, and a
  hold after a repeat failure until a window that started after it.
  "Schedule" on the page overrides the guards for one thread; "Now"
  resumes immediately.
- Threads that received a scheduled heartbeat (`<heartbeat>` user message)
  are automation threads and are never resumed automatically.
- Approval prompts from a turn we run are routed to Codex's guardian
  reviewer (`approvals: guardian`) or declined with a notification.

## Codex internals relied on (CLI 0.160.0)

- Binary: `/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex`.
- `~/.codex/state_5.sqlite` table `threads`; `~/.codex/thread_history_1.sqlite`
  tables `thread_turns` (status, error_json) and `thread_items`;
  `~/.codex/queue_1.sqlite` table `queued_items`.
- Open threads hold `~/.codex/thread-writer-locks/<id>.lock`.
- The app picks up a queued message for an idle open thread within
  seconds and starts the turn itself.

## Out of scope

ChatGPT web conversations, multiple accounts, Windows and Linux.
