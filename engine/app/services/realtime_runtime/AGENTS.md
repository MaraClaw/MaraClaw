# app/services/realtime_runtime

This package handles cross-process realtime routing. Local WebSocket connection ownership still lives in `app/api/websocket.py`.

## Routing Model

- `RealtimeRouter` uses Redis presence and Pub/Sub under the `realtime:ws` prefix.
- Messages are delivered locally first, then published to remote instance channels discovered from Redis presence records.
- `INSTANCE_ID` is part of routing identity. Set stable ids for multi-worker deployments that need predictable audit/routing behavior.
- Presence TTL is 180 seconds. Stale Redis connection ids are cleaned opportunistically during presence listing.

## Editing Rules

- Do not move local in-memory WebSocket connection state into this package without redesigning `app/api/websocket.py`.
- Do not create ad hoc Redis clients for realtime paths. Use the router/core lifecycle and ensure shutdown remains handled from `app.main`.
- Preserve JSON event shapes used by websocket clients and trigger notifications.
- Treat Redis failures as cross-process delivery failures; do not let them corrupt local connection state.
