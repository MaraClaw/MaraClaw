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
- `executions.py` owns claiming, first-start accounting, fenced five-minute leases, isolated renewal, recovery, and runtime trigger cloning.
- `invoker.py` holds leases during chat/LLM/delivery/audit orchestration, joins the heartbeat, then writes fenced terminal status.
- `keys.py` builds deterministic idempotency keys.

## Invariants

- Do not bypass `trigger_executions` for scheduled/webhook triggers. It provides idempotency, distributed claiming, and lease recovery.
- Each claim stores a unique lease token in `lease_owner`; renewal and terminal writes require `status = 'processing'` and that token.
- Heartbeats renew every `LEASE_RENEW_INTERVAL` in a fresh context with short transactions; never hold a checkout while sleeping or calling the LLM.
- Count `fire_count` once on first start inside the claim transaction, before invocation.
- Reclaim expired work for once/max-fires-disabled triggers; fail expired work for other disabled triggers. Claims run every tick.
- Reload trigger/agent rows via DAOs after a long LLM call; do not hold a stale `connection_ctx` across the invoke.
- Poll triggers must preserve the private/internal URL block; it is an SSRF boundary.
- `on_message` matching must continue excluding internal trigger reflection sessions to avoid loops.
- A2A wake depth, TTL, and dedup guards live in `trigger_daemon.py`, not this package.
- Webhook dedup depends on unique `(trigger_id, idempotency_key)` semantics.
