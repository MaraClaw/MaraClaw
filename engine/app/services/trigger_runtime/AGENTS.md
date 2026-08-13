# app/services/trigger_runtime

This package is the queued trigger execution runtime. It is coupled to agents, chat sessions, OKR automation, websocket delivery, audit logs, and A2A behavior.

## Startup Path

- `app.main.lifespan` starts `trigger_daemon.start_trigger_daemon()` only when `PROCESS_ROLE` includes `all` or `worker`.
- Do not move trigger scheduling into ordinary API processes. Duplicate workers increase scheduling pressure even though DB claiming mitigates execution duplication.
- `trigger_daemon.py` owns the 15-second process loop and roughly every-fourth-tick heartbeat bridge.

## Module Responsibilities

- `evaluator.py` owns trigger condition semantics for `cron`, `once`, `interval`, `poll`, `on_message`, and deterministic OKR handlers.
- `queue.py` inserts `trigger_executions` rows (`TriggerExecutionRecord`) with idempotency keys and webhook payload handling.
- `dispatch.py` converts due triggers into queued executions and claims ready invocations grouped by agent.
- `executions.py` owns `FOR UPDATE SKIP LOCKED` claiming, then one `UPDATE … WHERE id = ANY(...)` lease write, five-minute leases, runtime trigger cloning, and completion/failure marking.
- `invoker.py` creates trigger chat sessions, calls the agent LLM, persists messages, sends notifications, audits, and marks execution status.
- `keys.py` builds deterministic idempotency keys.

## Invariants

- Do not bypass `trigger_executions` for scheduled/webhook triggers. It provides idempotency, distributed claiming, and lease recovery.
- Preserve pre-update/mark-fired behavior before async LLM invocation so long calls do not refire every tick.
- Reload trigger/agent rows via DAOs after a long LLM call; do not hold a stale `connection_ctx` across the invoke.
- Poll triggers must preserve the private/internal URL block; it is an SSRF boundary.
- `on_message` matching must continue excluding internal trigger reflection sessions to avoid loops.
- A2A wake depth, TTL, and dedup guards live in `trigger_daemon.py`, not this package.
- Webhook dedup depends on unique `(trigger_id, idempotency_key)` semantics.
