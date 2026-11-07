# MaraClaw architecture

**Source snapshot: 2026-11-01; search integration updated 2026-11-07.** These views describe the checked-in implementation and launcher defaults, not a proposed architecture, live deployment proof, or a successful image build. Configuration and the running environment can differ from this snapshot.

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

Each view includes an SVG preview and expandable editable Mermaid source. Mermaid's **C4 support is experimental**, and its layout depends on the rendering environment. The SVG previews preserve the inspected layout even when a Markdown viewer cannot render C4 or uses a narrower screen.

All five diagrams were rendered with Mermaid CLI **12.0.0** and Chrome, then visually inspected for overlapping labels and clipping. All relative documentation and source paths were checked. To regenerate a preview, save its Mermaid block without the fence markers as `diagram.mmd`, then run:

```sh
npx --yes --package=@mermaid-js/mermaid-cli@12.0.0 mmdc \
  -i diagram.mmd -o diagram.svg -p puppeteer.json
```

Use a Puppeteer configuration like this, adjusting `executablePath` for your installed Chrome. The screen size is needed as well as the viewport to preserve the intended C4 columns:

```json
{
  "executablePath": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "args": ["--window-size=4000,2600", "--screen-info={4000x2600}"],
  "defaultViewport": {"width": 4000, "height": 2600}
}
```

After changing a diagram, regenerate its matching SVG and inspect the result before publishing.

Sources are linked relative to this directory (`../../` reaches the repository root); line fragments locate the reviewed ranges and can drift as code changes. The source review used the best-effort code graph plus direct reads, including nginx files that the graph could not parse and changed source where graph metadata was stale. No application services, application builds, application tests, or live integrations were exercised for this documentation-only change.
