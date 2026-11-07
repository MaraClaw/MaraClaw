# app/services/realtime_runtime

This package handles cross-process realtime routing. Local WebSocket connection ownership still lives in `app/api/websocket.py`.

## Routing Model

- `RealtimeRouter` (`router.py`) uses Redis presence and Pub/Sub under the `realtime:ws` prefix. `contracts.py` holds the fixed envelope and presence shapes; websocket event payloads stay unconstrained JSON.
- Messages go to the matching local sockets concurrently (`fanout.py`, at most `LOCAL_FANOUT_CONCURRENCY` in flight) while the Redis presence lookup runs alongside; remote instance channels are published after both finish. The event is serialized once per delivery (`encode_event_text`). The receiver (`deliver_pubsub_message`) uses the same fan-out, still one pub/sub message at a time in arrival order; never dispatch pub/sub messages as separate tasks, which would reorder chunks.
- `INSTANCE_ID` is part of routing identity. Set stable ids for multi-worker deployments that need predictable audit/routing behavior.
- Presence TTL is 180 seconds. Stale Redis connection ids are cleaned opportunistically during presence listing.

## Lifecycle And Readiness

- The lifecycle owner is `app.runtime.lifecycle` (the `api` role calls `realtime_router.start`, shutdown calls `stop`), not `app.main`. A start failure is reported and logged, and the process stays not-ready.
- `RealtimeRouter.ready` feeds `/api/ready` (`runtime/readiness.py`) only for the `api` role. It needs a started router, a live subscriber task, an **acknowledged** subscription and a connected Redis connection.
- `subscription.py` `AcknowledgedPubSub` clears `acknowledged` on health-check disconnect, `on_connect` and `_reconnect`, because redis-py retries reads inside its own reconnect boundary. The subscriber loop sets it only from a `subscribe` message for this instance's channel, and clears it on `unsubscribe`, loss and shutdown. Do not set readiness from a successful `subscribe()` call.

## Shared Chat Delivery

- `chat_authorization.py` `authorize_chat_delivery` runs for every local send. `read_shared_acl_version` reads the agent ACL version once per delivery, before any socket authorizes and only when a target is the shared session, and the caller passes it as `acl_version`; ordinary sessions do no ACL read. Ordinary sessions pass; a delivery targeting the agent's shared session needs the socket's `ChatTicketBinding` for that exact agent and session, then `chat_tickets.authorize` (fresh principal and `check_agent_access(..., fresh=True)`) and `authorized_session` reauthorize it. Any failure silently skips that socket.
- The receiving side runs the same check: `api/websocket.py` `deliver_pubsub_message` calls `authorize_chat_delivery` for pub/sub deliveries. Keep both call sites; do not move authorization to the publisher.
- `route_message(send_timeout=)` bounds only each `ws.send_text`; a timed-out socket is skipped like any failed send. The receiver applies the same bound (`CHUNK_SEND_TIMEOUT_SECONDS`) to chunk events only. Never put a deadline around `send_to_session` or `route_message` themselves: cancelling `authorize_chat_delivery` discards the positive decision it is about to cache, so every later chunk re-authorizes and is cancelled again, and cancelling routing skips remote instances.

## Editing Rules

- Do not move local in-memory WebSocket connection state into this package without redesigning `app/api/websocket.py`.
- Do not create ad hoc Redis clients for realtime paths. Use the router and `get_redis()`, and keep startup and shutdown in `app.runtime.lifecycle`.
- Preserve JSON event shapes used by websocket clients and trigger notifications.
- Treat Redis failures as cross-process delivery failures; do not let them corrupt local connection state.
- Tests: `tests/test_realtime_contracts.py`, `tests/test_realtime_readiness.py`, `tests/test_shared_chat_realtime.py`, `tests/test_chunk_live_delivery.py` (slow-but-healthy authorization/Redis, stalled socket, bounded stream drain).
