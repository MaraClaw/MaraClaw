# MaraClaw architecture

**Source snapshot: 2026-11-01.** These views describe the checked-in implementation and launcher defaults, not a proposed architecture, live deployment proof, or a successful image build. Configuration and the running environment can differ from this snapshot.

## Read the views

| View | Audience and scope |
| --- | --- |
| [System context](c4-context.md) | People, the platform, and optional external integrations |
| [Containers](c4-containers.md) | Independently built applications, guest runtimes, and state boundaries |
| [Engine components](c4-components-engine.md) | Focused runtime ownership, intake, dispatch, and reply settlement |
| [Deployment](c4-deployment.md) | Topology encoded by the three package launchers, not provisioned infrastructure |
| [Chat flow](c4-dynamic-chat.md) | Ticket authorization, durable acceptance, guest fallback, and reply delivery |

Start with context and containers. Use deployment to distinguish a browser application from its nginx server, and the chat view to distinguish committed work from best-effort notifications.

## Reading conventions

- Each view contains one Mermaid C4 diagram with fewer than 20 elements. Arrows are directional and describe an action and its protocol or in-process interface. Optional integrations are capabilities, not evidence that credentials or services are configured.
- A C4 container means an application/runtime or data boundary, not necessarily a Docker container. React libraries are implementation details. The independently launched packages are [engine](../../engine/start-from-docker.sh), [web-e](../../web-e/start-from-docker.sh), and [web-a](../../web-a/start-from-docker.sh).
- Engine roles control startup side effects, not separate services automatically deployed by the launchers: [default role](../../engine/app/config.py#L162-L167), [lifecycle owner](../../engine/app/runtime/lifecycle.py#L66-L146), and [unconditional router mounts](../../engine/app/main.py#L130-L191).
- PostgreSQL owns durable chat/work records; Redis provides tickets, presence, and cross-instance Pub/Sub. See [acceptance](../../engine/app/services/chat_submissions.py#L69-L134), [settlement](../../engine/app/services/openclaw_gateway_delivery.py#L73-L167), [tickets](../../engine/app/services/chat_tickets.py#L70-L121), and [realtime routing](../../engine/app/services/realtime_runtime/router.py#L63-L165).

## Preview and validation

Mermaid's **C4 support is experimental**. A Markdown renderer without the relevant C4 support may display the fenced source or reject a view. Before publishing, preview/render all five diagrams locally with a C4-capable Mermaid integration and inspect node placement, arrow labels, clipping, and readability. Text/source review alone does not establish rendered layout quality; adjust layout only after viewing the render. No SVG artifacts are included in this snapshot.

Sources are linked relative to this directory (`../../` reaches the repository root); line fragments locate the reviewed ranges and can drift as code changes. The source review used the best-effort code graph plus direct reads, including nginx files that the graph could not parse and changed source where graph metadata was stale. No services, builds, tests, or live integrations were exercised for these documents; rendering and visual review are a separate follow-up.
