# app/services/trigger_runtime

This package is the queued trigger execution runtime. It is coupled to agents, chat sessions, OKR automation, websocket delivery, audit logs, and A2A behavior.

## Startup Path

- `app.runtime.lifecycle.Runtime.start()` starts `trigger_daemon.start_trigger_daemon()` only when `PROCESS_ROLE` includes `all` or `worker`.
- Do not move trigger scheduling into ordinary API processes. Duplicate workers increase scheduling pressure even though DB claiming mitigates execution duplication.
- `trigger_daemon.py` owns the 15-second trigger loop; the worker lifecycle separately owns the 60-second heartbeat loop.
- `scan.py` (`TriggerScanner`) reads enabled triggers through `agent_trigger_dao.list_enabled_page` (id-ordered keyset pages, never larger than the free slots) and owns at most `MAX_CONCURRENT_EVALUATIONS` tasks. Its cursor persists across ticks and wraps after the last page, so a trigger deferred for capacity is read on a later tick and hung ones cannot starve high ids. A tick waits `EVALUATION_SETTLE_SECONDS` in total, not per page. `close()` cancels and awaits every owned task.
- `trigger_daemon.EVALUATION_DEADLINE_SECONDS` bounds only `_evaluate_trigger` (`_evaluate_within_deadline`). Never widen it to `_process_trigger`: the OKR report/collection handlers send IM messages before `mark_trigger_fired`, and `enqueue_due_trigger` owns the poll baseline transaction, so cancelling them duplicates sends. A timed-out poll keeps its claimed checkpoint and waits for its next interval; a cron trigger catches up from `last_fired_at`. The daemon beats `loop_progress()` only after a tick finishes without raising (`PROGRESS_BUDGET_SECONDS`).

## Module Responsibilities

- `evaluator.py` owns trigger condition semantics for `cron`, `once`, `interval`, `poll`, `on_message`, and deterministic OKR handlers. `poll_check` returns a `PollObservation` and never writes config.
- `queue.py` inserts `trigger_executions` rows (`TriggerExecutionRecord`) with idempotency keys and webhook payload handling.
- `dispatch.py` converts due triggers into queued executions (polls via `_enqueue_poll_observation`) and claims ready invocations grouped by agent.
- `executions.py` owns claiming, first-start accounting, fenced five-minute leases, the lease hold that renews and stops lost work, recovery, and runtime trigger cloning.
- `invoker.py` runs chat/LLM/delivery/audit inside the lease hold, checks the hold before each side effect, confirms ownership after the LLM returns, joins the heartbeat, then writes fenced terminal status.
- `keys.py` builds deterministic idempotency keys and owns `poll_sequence` / `POLL_OBSERVED_KEY`.

## Invariants

- Do not bypass `trigger_executions` for scheduled/webhook triggers. It provides idempotency, distributed claiming, and lease recovery.
- `try_enqueue` locks the trigger `FOR KEY SHARE`, then the agent `FOR KEY SHARE NOWAIT`, so it never waits on an agent delete that cascades back to the trigger. After `ENQUEUE_LOCK_ATTEMPTS` busy attempts it raises `LockNotAvailableError`, never `(None, False)`, which callers read as a duplicate; webhooks answer 503. Call it from a transaction that holds no other row locks.
- Each claim stores a unique lease token in `lease_owner`; renewal and terminal writes require `status = 'processing'` and that token.
- Claims lock executions and their trigger rows with `SKIP LOCKED` and never wait on a row lock. Trigger deletes lock the trigger before cascading to its executions, so a waiting claim can deadlock.
- Renewal and completed/failed writes update one execution per transaction in a fresh context. Do not batch them or run them on an ambient `connection_ctx`.
- Heartbeats renew every `LEASE_RENEW_INTERVAL`; never hold a checkout while sleeping or calling the LLM.
- The hold confirms ownership before the invocation starts and cancels it when any execution is lost or no renewal succeeds `LEASE_STOP_MARGIN` before expiry. Deadlines count from before the renewal I/O and advance only when every execution renewed.
- Call `hold.confirm()` after the LLM returns, before persisting or delivering the reply. On lease loss, fail the still-owned rows with `LEASE_LOST_ERROR`. External cancellation wins and leaves the rows to lease recovery.
- Pass `hold.ensure_held` as `side_effect_guard` to `call_llm` and as `guard` to the delivery helpers, and call it right before each invoker write and push. It raises `ExecutionLeaseLostError` once the monotonic or the wall-clock deadline has passed (a suspended host stops the monotonic clock while the database lease keeps expiring). Re-raise `ExecutionLeaseLostError` before any broad `except`.
- Ticked and woken invocations start in an empty context through `trigger_daemon._start_invocation`; never let them inherit a caller's connection, tool context, or trace.
- Side effects are at least once, not exactly once. A stalled owner stops at its next guarded model round, tool call, write, or push, but work it already admitted (a running tool, thread work, a sent request) is not undone, clock skew larger than `LEASE_STOP_MARGIN` can still overlap the new owner, and work that crashes before its terminal write goes to lease recovery.
- Count `fire_count` once on first start inside the claim transaction, before invocation.
- `agent_triggers.self_disabled` records that a trigger turned itself off through `once` or `max_fires`. Only the fire-accounting updates (`record_first_starts`, `evaluator.mark_trigger_fired`) set it, on the enabled-to-disabled transition. `AgentTriggerDAO.update` clears it whenever `is_enabled` is written, and the tenant/creator bulk disables clear it. Do not expose it in API responses or trigger config.
- Reclaim expired work when its trigger is enabled or `self_disabled`; fail expired work for every other disabled trigger. Claims run every tick.
- Reload trigger/agent rows via DAOs after a long LLM call; do not hold a stale `connection_ctx` across the invoke.
- Poll triggers use the shared core public-destination policy and pinned HTTP client; preserve this SSRF boundary and the existing no-redirect behavior.
- `on_message` matching must continue excluding internal trigger reflection sessions to avoid loops.

## Event Identity

- Poll state lives in trigger config JSON (no columns): `_last_value` baseline, monotonic `_poll_seq` (absent = 0), and `_poll_checked_at` last claim time. `_poll_observed` is in-memory only, set by `evaluate_trigger` for `enqueue_due_trigger`; never persist it.
- A poll change enqueues, then `agent_trigger_dao.advance_poll_baseline(expected_seq=...)` in the same `connection_ctx` transaction, after `try_enqueue`. Zero rows matched → `PollBaselineLostError` rolls the execution back; the loser never adopts its observation. Enqueue failure leaves the baseline, so the next poll re-sees the change.
- First poll and non-entering `fire_on=match` values only advance the baseline (still conditional, never an execution). Match fires each time the value enters `match_value`.
- Keys: poll `poll:{trigger}:{_poll_seq + 1}` (A-B-A-B → three runs); `on_message` `on_message:{trigger}:{_matched_message_id}`. Never hash values or message text.
- Never write internal underscore keys through `BaseDAO.update` (whole-config replace rolls the baseline back). User edits go through `merge_user_config` (SQL-side merge; `url`/`json_path` change drops `_last_value` and `_poll_checked_at` and bumps `_poll_seq`). The agent-facing trigger update tool still replaces config wholesale.
- `from_agent_name` resolves only agents in the trigger agent's tenant.
- A2A wake depth, TTL, and dedup guards live in `trigger_daemon.py`, not this package.
- Webhook dedup depends on unique `(trigger_id, idempotency_key)` semantics.
