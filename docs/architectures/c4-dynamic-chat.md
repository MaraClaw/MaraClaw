# Durable member chat flow

[Architecture index](README.md) | Source snapshot: **2026-11-01**

This view follows a normal member submission through ticket authorization, durable gateway work, and reply settlement. It omits attachment handling and onboarding triggers. Nginx transport is collapsed into the client-to-engine arrow; see the [deployment view](c4-deployment.md) for the actual proxy hop.

![MaraClaw durable member chat flow](c4-dynamic-chat.svg)

<details>
<summary>Editable Mermaid C4 source</summary>

```mermaid
%%{init: {"c4": {"c4ShapeMargin": 170, "c4ShapePadding": 20, "width": 260, "height": 100, "boxMargin": 30, "diagramMarginX": 60, "diagramMarginY": 60}}}%%
C4Dynamic
  title MaraClaw - Member chat acceptance and reply

  Container(memberApp, "Member SPA", "React browser client", "Opens chat and submits an identified turn")
  ContainerDb(redis, "Ephemeral coordination", "Redis", "Single-use tickets and realtime routing")
  ContainerDb(postgres, "Durable records", "PostgreSQL", "Transcript, quota, session, gateway state")
  Container(guest, "Agent guest", "OpenClaw container", "Executes work and reports the answer")

  Container_Boundary(engine, "engine") {
    Component(ingress, "Chat ingress", "FastAPI", "Authenticates HTTP tickets and WebSockets")
    Component(submit, "Submission and dispatch", "Python", "Prepares, commits, then attempts wake")
    Component(gateway, "Gateway settlement", "FastAPI, Python", "Claims work and commits the winning report")
    Component(realtime, "Realtime delivery", "Python", "Best-effort local and remote live delivery")
  }

  Rel(memberApp, ingress, "Mint ticket; connect; submit", "Bearer HTTP / ticketed WS")
  Rel(ingress, redis, "Issue / consume ticket", "Redis SET + expiry / GETDEL")
  Rel(ingress, submit, "Accept authorized turn", "Python calls")
  Rel(submit, postgres, "Commit accepted turn / work", "SQL / one transaction")
  Rel(submit, guest, "Attempt guarded wake", "Docker exec / guest hook")
  Rel(guest, gateway, "Poll eligible work; report", "HTTP / JSON; X-Api-Key")
  Rel(gateway, postgres, "Claim / commit winning report", "SQL / transactions")
  Rel(gateway, realtime, "Attempt notification", "Python / after commit")
  Rel(realtime, redis, "Route remote events", "Redis presence / Pub/Sub")
  Rel(realtime, memberApp, "Deliver if connected", "WebSocket / JSON")

  UpdateLayoutConfig($c4ShapeInRow="4", $c4BoundaryInRow="1")
  UpdateRelStyle(memberApp, ingress, $offsetX="-150", $offsetY="-80")
  UpdateRelStyle(ingress, redis, $offsetX="-120", $offsetY="100")
  UpdateRelStyle(ingress, submit, $offsetX="-80", $offsetY="50")
  UpdateRelStyle(submit, postgres, $offsetX="-120", $offsetY="100")
  UpdateRelStyle(submit, guest, $offsetX="300", $offsetY="-120")
  UpdateRelStyle(guest, gateway, $offsetX="70", $offsetY="90")
  UpdateRelStyle(gateway, postgres, $offsetX="20", $offsetY="-150")
  UpdateRelStyle(gateway, realtime, $offsetX="-70", $offsetY="50")
  UpdateRelStyle(realtime, redis, $offsetX="100", $offsetY="220")
  UpdateRelStyle(realtime, memberApp, $offsetX="-250", $offsetY="-240")
```

</details>

## Sequence and guarantees

1. **Ticket and socket.** Bearer-authenticated `POST /api/auth/chat-ticket` issues a Redis-backed, single-use ticket; the browser then connects to `/ws/chat/{agent_id}` with the bound session and ticket, not a JWT in the URL. Redemption uses `GETDEL`. Setup and each submitted turn freshly reauthorize the principal, tenant, agent, and session. Steps 1-2 compress this handshake, not parallel actions: [ticket endpoint](../../engine/app/api/auth.py#L72-L82), [ticket service](../../engine/app/services/chat_tickets.py#L47-L121), [socket setup](../../engine/app/api/websocket.py#L201-L299), [per-turn checks](../../engine/app/api/websocket.py#L329-L378).
2. **Durable acceptance before acknowledgement.** `accept_submission` locks the session, checks a submission UUID/fingerprint for replay or conflict, prepares guest configuration, reserves the user transcript, charges quota, updates the session, and inserts gateway work within the owning transaction. Guest configuration is a filesystem side effect, not part of database rollback. Only after successful acceptance does the socket send `turn_accepted`; that acknowledges acceptance, not a finished guest answer: [submission transaction](../../engine/app/services/chat_submissions.py#L69-L134), [model/config preparation](../../engine/app/services/openclaw_routing.py#L76-L152), [socket acknowledgement](../../engine/app/api/websocket.py#L461-L534).
3. **Wake and poll are alternative dispatch paths.** The post-commit wake attempts a bootstrap-guarded Docker-exec hook. Failure does not discard committed work. Ordinary claims can be returned to pending or reclaimed after the configured stale-delivery timeout, allowing authenticated `GET /api/gateway/poll` to pick them up. A shared conversation instead uses a serialized shared-lane claim: stale ordinary-delivery requeue explicitly excludes shared turns; a shared wake is released only when bootstrap refusal proves it was not admitted. The polling edge does not imply unconditional retry of every shared turn: [wake/claim handling](../../engine/app/services/openclaw_routing.py#L187-L248), [guarded hooks and shared CLI fallback](../../engine/app/services/openclaw_inbox.py#L332-L426), [poll claims](../../engine/app/services/openclaw_gateway_delivery.py#L43-L70), [poll endpoint](../../engine/app/api/gateway.py#L60-L138).
4. **Completion is durable; live notification is not.** The guest reports through `POST /api/gateway/report` using `X-Api-Key`. First-report-wins settlement commits gateway completion and, for a non-empty session answer, the assistant transcript together with applicable IM/agent delivery intents. Duplicate reports do not append duplicate answers. The subsequent `done` event is best effort: local sockets are served directly and remote instances use Redis presence/PubSub; a missed notification does not undo the transcript. History is read from persisted messages: [report endpoint](../../engine/app/api/gateway.py#L178-L200), [settlement/publication](../../engine/app/services/openclaw_gateway_delivery.py#L73-L167), [realtime routing](../../engine/app/services/realtime_runtime/router.py#L115-L218), [history loading](../../engine/app/api/websocket.py#L302-L319).

The diagram is not a token-streaming promise or an exactly-once guest-execution guarantee. Configured guest inference is separate from [engine-side LLM clients](../../engine/app/services/llm/factory.py#L50-L116). When settlement creates IM/agent reply intents, the [worker relay](../../engine/app/services/gateway_report_relay.py#L116-L262) handles them durably outside this member-only flow; Pub/Sub is not that delivery ledger.
