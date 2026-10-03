# codex-resume: auto-continue Codex threads after a usage-limit reset

Date: 2026-10-03. Status: approved design (option B: local web page + notifications).

## Problem

The ChatGPT desktop app (Codex engine, CLI 0.160.0) stops a thread with
`usageLimitExceeded` when the 5-hour window is used up. The window resets
hours later, often while the user is away, so the fresh window is wasted until
the user notices and types "continue". Goal: detect blocked threads, show when
they will resume, and continue them automatically the moment the window
resets, one at a time, with a weekly-budget guard.

## Verified facts the design relies on

- The desktop runs a private `codex app-server` engine (stdio pipe). We cannot
  connect to it, but the same binary can run our own engine over stdio
  (`codex app-server --listen stdio://`) sharing `~/.codex`.
  Binary: `/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex`.
- Our engine answers `initialize`, `account/rateLimits/read` (primary 5h and
  secondary weekly windows: `usedPercent`, `resetsAt` epoch seconds,
  `ordinaryUsageAllowed`), `thread/list` (`useStateDbOnly: true`, no locks),
  `thread/read`, `thread/turns/list`.
- A failed turn carries `error.codexErrorInfo == "usageLimitExceeded"` and a
  message ending "try again at 1:56 PM".
- Open threads in the desktop hold `~/.codex/thread-writer-locks/<id>.lock`.
  `thread/resume` from our engine then fails with "already has an active
  writer".
- `codex queue --thread <id> --message <text>` stores a durable message in
  `~/.codex/queue_1.sqlite`. Verified: when the thread is next loaded by any
  engine (`thread/resume` in ours), the message is consumed automatically and
  starts a turn. Docs say an engine that already has the thread loaded and
  idle also wakes it (unverified on 0.160.0).
- Approval reviewers available on resume: `user`, `auto_review`,
  `guardian_subagent`. The desktop already uses guardian review for this user.

## Architecture

```
                 codex-resume (python3, stdlib only, launchd at login)
 ┌─────────────────────────────────────────────────────────────────────┐
 │ scheduler loop (tick 60s)                                           │
 │   limits   <- engine account/rateLimits/read   (every 5 min, 60s    │
 │                                                 near reset)         │
 │   blocked  <- detector: thread/list + last turn via thread/turns/list│
 │   plan     <- pure fn(blocked, limits, state, now) -> actions       │
 │   deliver  -> queue + wake (one thread at a time)                   │
 │   notify   -> osascript display notification                        │
 │   state    -> ~/.codex-resume/state.json (skips, messages, history) │
 ├─────────────────────────────────────────────────────────────────────┤
 │ web ui  http://127.0.0.1:8765  (limits, queue table, skip/now/edit) │
 └───────────────┬─────────────────────────────────────────────────────┘
                 │ stdio JSON-RPC
         our engine (codex app-server)  ──shares──►  ~/.codex
                                                     ▲
         ChatGPT.app ── desktop engine ──────────────┘
```

### Delivery sequence

```
scheduler        codex queue        our engine         desktop engine
   | resume_at reached, usage allowed                        |
   |── queue "continue" ──► queue db                         |
   |   lock held by desktop?                                 |
   |     yes: wait <=90s for a new turn ─────────────────────►| drains if idle+loaded
   |          none: state=pending-in-app, notify user         |
   |     no:  thread/resume(model, effort, reviewer) ──►|     |
   |          queue drains, turn/started ... turn/completed   |
   |          thread/unsubscribe (lock released)              |
   |   next blocked thread                                    |
```

## Components

| Module | Responsibility | Depends on |
|---|---|---|
| `engine.py` | Spawn `codex app-server --listen stdio://`, JSON-RPC ids, notification stream, answer server requests (approvals) per policy, restart on crash | subprocess |
| `limits.py` | `account/rateLimits/read` to a `Limits` dataclass (primary/secondary used %, resets_at, allowed) | engine |
| `detector.py` | List candidate threads (sourceKinds cli, vscode, appServer; not archived; last 50 by updated_at), fetch last turn, classify `Blocked(thread_id, cwd, title, last_ask, blocked_at, reset_hint)` | engine |
| `planner.py` | Pure: `(blocked, limits, state, now) -> Plan` with per-thread `resume_at`, order, `next_action` | none |
| `deliver.py` | `codex queue`, lock check, wake via our engine, wait for turn completion, outcome | engine, subprocess |
| `state.py` | JSON file: per-thread skip flag, custom message, attempts, history events (cap 200) | none |
| `web.py` | `http.server` on 127.0.0.1:8765. GET `/` page, GET `/api/state`, POST `/api/threads/<id>/skip`, `/unskip`, `/resume-now`, `/message` | state, scheduler |
| `notify.py` | macOS notification via osascript | subprocess |
| `scheduler.py` | Tick loop wiring the above | all |
| `__main__.py` | `run` (loop + web), `status` (table), `install-launchd` / `uninstall-launchd` | all |

## Behaviour rules

- Blocked = last turn `failed` with `codexErrorInfo == usageLimitExceeded` and
  no later turn. Subagent threads (`subAgent*` source kinds, `parentThreadId`
  set) are ignored.
- `resume_at = max(now, primary.resets_at) + 60s`. Threads resume oldest
  blocked first, one at a time. The next starts when the previous turn
  completes or fails.
- Guards: skip auto-resume while `secondary.usedPercent >= weekly_guard`
  (default 90); at most `max_attempts` (default 3) automatic resumes per
  thread per day; "Resume now" in the UI bypasses both.
- A resumed turn that fails again with `usageLimitExceeded` re-blocks the
  thread and reschedules everything to the new `resets_at`.
- Default message is "continue"; editable per thread in the UI. The original
  failed user message is already in the thread history, so no re-send.
- Our-engine wake passes the thread's recorded `model` and `reasoningEffort`
  and `approvalsReviewer: guardian_subagent` (config `approvals: guardian |
  decline`). Any approval request that still reaches us is declined and the
  user is notified with the thread name.
- Lock release: `thread/unsubscribe` right after `turn/completed` so the
  desktop can take the thread back.
- Reset time fallback: if limits are unavailable, parse "try again at H:MM AM/PM"
  from the error message in local time.

## Web page (option B)

Single HTML page, vanilla JS polling `/api/state` every 10s, no build step.

```
 codex-resume                                   engine: ok  updated 13:52:10
 5h window   ████████████████████ 100%  resets 13:56 (in 4 min)
 weekly      ████                 20%   resets Thu 9 Oct 22:28

 Project          Last ask                      Blocked   Resume at  State      Msg
 cleverplayhouse  I want all 51 games visible…  09:30     13:57 #1   scheduled  continue  [Skip][Now][Edit]
 cleverplayhouse  continue (website feedback)   09:30     after #1   scheduled  continue  [Skip][Now][Edit]

 History
 13:57:05  queued "continue" -> 01a0fe2e  (wake: our engine)
 13:57:09  turn started 01a0fe2e
```

## Configuration

`~/.codex-resume/config.json`, all optional: `codex_bin`, `port` (8765),
`weekly_guard` (90), `max_attempts` (3), `approvals` ("guardian"),
`default_message` ("continue"), `grace_seconds` (60).

## Error handling

- Engine missing or crashing: UI shows "engine: down", tick retries with
  backoff (10s to 5 min), nothing is delivered until limits can be read.
- `codex queue` non-zero exit: record in history, retry next tick.
- Desktop holds the lock and does not drain within 90s: state
  `pending-in-app`, one notification, no further queueing for that thread
  (the queued message will fire when the user opens it).
- Clock/timezone: all times stored as epoch seconds, rendered in local time.

## Testing

- Unit (pytest-free, `unittest`): planner ordering, guards, re-block logic;
  detector classification from recorded `thread/turns/list` fixtures; reset
  time parsing; state round-trip; web API handlers via `http.client`.
- Integration smoke (skipped when the binary is absent): engine initialize
  and `account/rateLimits/read`.
- Canary: first real run today at the 13:56 BST reset on the two blocked
  cleverplayhouse threads.

## Out of scope

ChatGPT web conversations, multiple accounts, menu bar app, Windows/Linux,
predicting when a window will be exhausted.
