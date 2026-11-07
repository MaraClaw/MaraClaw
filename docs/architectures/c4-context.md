# System context

[Architecture index](README.md) | Source snapshot: **2026-11-01**; Tavily integration updated **2026-11-07**.

MaraClaw provides a public/member workspace and an operator console around OpenClaw-backed digital employees. The actors and product surfaces follow the [member routes](../../web-e/src/routes/index.tsx#L48-L106) and [admin routes](../../web-a/src/routes/index.tsx#L56-L105).

![MaraClaw system context](c4-context.svg)

<details>
<summary>Editable Mermaid C4 source</summary>

```mermaid
%%{init: {"c4": {"c4ShapeMargin": 150, "c4ShapePadding": 20, "width": 260, "height": 100, "boxMargin": 30, "diagramMarginX": 60, "diagramMarginY": 60}}}%%
C4Context
  title MaraClaw - System context

  Person(member, "Visitor or member", "Discovers and works with digital employees")
  System_Ext(identity, "Identity providers", "Optional configured OAuth sign-in")
  Person(operator, "Organization or platform admin", "Manages company and platform settings")
  System_Ext(email, "Email servers", "Configured email delivery and mailbox access")
  System(platform, "MaraClaw", "Multi-tenant digital employee platform")
  System_Ext(im, "IM platforms", "Configured conversations and replies")
  System_Ext(models, "Model endpoints", "Configured inference providers")
  System_Ext(search, "Tavily", "Optional web search API")

  Rel(member, platform, "Collaborates", "HTTP(S) / WebSocket")
  Rel(operator, platform, "Administers policy", "HTTP(S)")
  Rel(platform, identity, "Authenticates", "OAuth / HTTPS")
  Rel(platform, email, "Sends and reads mail", "SMTP / IMAP")
  Rel(im, platform, "Delivers events", "HTTPS / provider streams")
  Rel(platform, im, "Sends replies", "Provider transports")
  Rel(platform, models, "Requests inference", "HTTP APIs")
  Rel(platform, search, "Requests web search", "HTTPS / JSON")

  UpdateLayoutConfig($c4ShapeInRow="3", $c4BoundaryInRow="1")
  UpdateRelStyle(member, platform, $offsetX="-100", $offsetY="-20")
  UpdateRelStyle(operator, platform, $offsetX="80", $offsetY="-20")
  UpdateRelStyle(platform, identity, $offsetX="20", $offsetY="-20")
  UpdateRelStyle(platform, email, $offsetX="-90", $offsetY="-45")
  UpdateRelStyle(im, platform, $offsetX="-70", $offsetY="-45")
  UpdateRelStyle(platform, im, $offsetX="-70", $offsetY="35")
  UpdateRelStyle(platform, models, $offsetX="-100", $offsetY="10")
  UpdateRelStyle(platform, search, $offsetX="20", $offsetY="10")
```

</details>

## Sources and boundaries

- Identity is optional: the [provider registry](../../engine/app/services/auth_provider.py#L915-L923) includes Feishu, DingTalk, WeCom, Google Workspace, Microsoft Teams, Google, and GitHub. This is a sign-in boundary, not a provider organization-directory sync service.
- Email covers [SMTP transport](../../engine/app/core/email.py#L67-L134) and [per-agent SMTP/IMAP configuration](../../engine/app/services/email_service.py#L1-L59); it is not modeled as a universal inbound chat channel.
- IM groups provider-specific transports rather than claiming uniform features. [Connector managers](../../engine/app/runtime/lifecycle.py#L39-L52) cover Feishu, DingTalk, WeCom, WeChat, and Discord; [mounted channel routes](../../engine/app/main.py#L162-L170) also expose webhook/API integrations. The Personal WhatsApp bridge is an engine-owned child transport, not a separate public server: [lifecycle](../../engine/app/runtime/lifecycle.py#L128-L139), [child protocol](../../engine/app/services/channels/whatsapp_personal_child.py#L42-L80).
- Model endpoints serve both configured OpenClaw guests and engine-side features. [Guest configuration](../../engine/app/services/openclaw_routing.py#L76-L125) is distinct from the [engine LLM client factory](../../engine/app/services/llm/factory.py#L50-L116); guest answers are not assumed to traverse that factory.
- Tavily is backed by the [upstream client](../../engine/app/services/tavily/client.py) and [engine proxy](../../engine/app/api/tavily_proxy.py). Search uses Tavily's `query` and `search_depth` request fields and `results` response. [Guest configuration](../../engine/app/services/agent_manager.py) supplies the agent-bound proxy endpoint and token; bundled guest skills and the guest image recipe are maintained outside this repository.

Internal databases, Redis, and guest processes are deliberately deferred to the [container view](c4-containers.md).
