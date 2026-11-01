# Checked-in launcher topology

[Architecture index](README.md) | Source snapshot: **2026-11-01**

This diagram describes the default topology encoded by [engine](../../engine/start-from-docker.sh#L129-L214), [web-e](../../web-e/start-from-docker.sh#L29-L32), and [web-a](../../web-a/start-from-docker.sh#L29-L32) launchers. It is not evidence that these containers, databases, or network paths are running. No cloud, Compose, Kubernetes, TLS terminator, or database provisioning is implied.

![MaraClaw checked-in launcher topology](c4-deployment.svg)

<details>
<summary>Editable Mermaid C4 source</summary>

```mermaid
%%{init: {"c4": {"c4ShapeMargin": 130, "c4ShapePadding": 20, "width": 260, "height": 100, "boxMargin": 30, "diagramMarginX": 60, "diagramMarginY": 60}}}%%
C4Deployment
  title MaraClaw - Checked-in launcher defaults

  Container(memberSpa, "Member SPA instance", "Browser: React, TypeScript", "Client-executed public and member UI")
  ContainerDb(postgres, "PostgreSQL", "Operator-supplied service", "Configured endpoint; durable records")
  ContainerDb(redis, "Redis", "Operator-supplied service", "Configured endpoint; tickets, presence, Pub/Sub")
  Container(adminSpa, "Admin SPA instance", "Browser: React, TypeScript", "Client-executed operator UI")

  Deployment_Node(host, "Container host", "Docker or Podman launcher", "Shared host paths and container network") {
    Deployment_Node(network, "maraclaw_network", "Configurable container network", "Independent package containers and guests") {
      Container(memberServer, "web-e server", "nginx :8080", "Serves dist; host port 5173")
      Container(engine, "maraclaw-engine", "Uvicorn :8000", "Host port 8000; default role all")
      Container(adminServer, "web-a server", "nginx :8080", "Serves dist; host port 5174")
      Deployment_Node(guestRuntimes, "Dynamic guest runtimes", "Same container network", "Managed sibling containers") {
        Container(guests, "OpenClaw guest instances", "OpenClaw gateway containers", "Created dynamically per agent")
      }
    }
    Deployment_Node(mounts, "Host mounts", "Host filesystem", "Shared DATA_DIR paths") {
      ContainerDb(data, "DATA_DIR", "Host filesystem", "Engine data and per-agent bind mounts")
    }
  }

  Rel(memberSpa, memberServer, "Loads / calls / chats", "HTTP / WS; host :5173")
  Rel(adminSpa, adminServer, "Loads / calls", "HTTP; host :5174")
  Rel(memberServer, engine, "Proxies /api, /ws, /p", "HTTP / WS; DNS :8000")
  Rel(adminServer, engine, "Proxies /api", "HTTP; DNS :8000")
  Rel(engine, postgres, "Connects to records", "SQL / psycopg")
  Rel(engine, redis, "Coordinates state", "Redis protocol")
  Rel(engine, guests, "Creates and execs", "Docker CLI / daemon socket")
  Rel(guests, engine, "Polls and reports", "HTTP / JSON; DNS :8000")
  Rel(engine, data, "Mounts /data + host path", "Bind mounts / filesystem")
  Rel(guests, data, "Mounts agent directory", "Bind: /home/node/.openclaw")

  UpdateLayoutConfig($c4ShapeInRow="4", $c4BoundaryInRow="1")
  UpdateRelStyle(memberSpa, memberServer, $offsetX="-140", $offsetY="-100")
  UpdateRelStyle(adminSpa, adminServer, $offsetX="60", $offsetY="-100")
  UpdateRelStyle(memberServer, engine, $offsetX="-90", $offsetY="-45")
  UpdateRelStyle(adminServer, engine, $offsetX="-70", $offsetY="-45")
  UpdateRelStyle(engine, postgres, $offsetX="-160", $offsetY="-100")
  UpdateRelStyle(engine, redis, $offsetX="40", $offsetY="-160")
  UpdateRelStyle(engine, guests, $offsetX="-160", $offsetY="-110")
  UpdateRelStyle(guests, engine, $offsetX="100", $offsetY="-10")
  UpdateRelStyle(engine, data, $offsetX="120", $offsetY="240")
  UpdateRelStyle(guests, data, $offsetX="-180", $offsetY="-40")
```

</details>

## Defaults and prerequisites

| Concern | Checked-in behavior and source |
| --- | --- |
| Package serving | Frontend defaults are host `5173 -> 8080` and `5174 -> 8080`; engine is `8000 -> 8000`. Overrides are launcher inputs: [web-e](../../web-e/start-from-docker.sh#L29-L32), [web-a](../../web-a/start-from-docker.sh#L29-L32), [engine](../../engine/start-from-docker.sh#L63-L67). Node builds the assets; nginx serves them: [web-e image](../../web-e/Dockerfile#L43-L71), [web-a image](../../web-a/Dockerfile#L55-L83). |
| Shared network | All three launchers create/join `DOCKER_NETWORK` (default `maraclaw_network`). Engine receives DNS alias `maraclaw-engine`: [engine network and run arguments](../../engine/start-from-docker.sh#L129-L183), [member launcher](../../web-e/start-from-docker.sh#L72-L87), [admin launcher](../../web-a/start-from-docker.sh#L72-L87). Start engine before frontend nginx resolves the upstream. |
| Proxy paths | Member nginx proxies `/api/`, `/ws/` with upgrade headers, and `/p/`; admin proxies `/api/` only: [member config](../../web-e/docker/nginx.conf#L62-L90), [admin config](../../web-a/docker/nginx.conf#L65-L74). These checked-in listeners use HTTP; production HTTPS needs separately supplied infrastructure. |
| Dependencies | Supply reachable `DATABASE_URL` and `REDIS_URL`; launchers do not start database or Redis servers: [settings](../../engine/app/config.py#L97-L108), [engine run configuration](../../engine/start-from-docker.sh#L129-L229). Their placement in the diagram is intentionally unspecified. |
| Host path identity | Engine mounts `DATA_DIR` at both `/data` and the same absolute host path, and pins `AGENT_DATA_DIR` and `STORAGE_LOCAL_ROOT` to its `agents` directory. This lets sibling guest mounts resolve on the daemon host: [launcher](../../engine/start-from-docker.sh#L129-L183), [guest mount](../../engine/app/services/agent_manager.py#L671-L685). |
| Docker control | Launcher mounts a Linux Docker CLI and mounts the daemon socket when available; without it, guest creation fails: [launcher](../../engine/start-from-docker.sh#L189-L214). Guests join the shared network and publish their gateway only on host loopback `127.0.0.1:<allocated-port>`: [guest run arguments](../../engine/app/services/agent_manager.py#L671-L685). |

The default is one engine with `PROCESS_ROLE=all`, not separate deployed API/worker/connector services: [setting](../../engine/app/config.py#L162-L183), [startup](../../engine/app/runtime/lifecycle.py#L66-L146). Schema bootstrap runs before Uvicorn for `all`/`bootstrap`: [entrypoint](../../engine/entrypoint.sh#L68-L96). Optional [S3-compatible file storage](../../engine/app/services/storage_runtime/facade.py#L17-L43), external integrations, and the engine-owned Personal WhatsApp child are omitted here to keep the launcher topology focused. Guest image construction is external to this repository; the engine consumes a configured image and manages instances. No build or live readiness claim is made.
