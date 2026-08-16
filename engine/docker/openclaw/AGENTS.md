# docker/openclaw

Guest **OpenClaw agent image** helpers. Not the FastAPI API. Image contract also lives in repo-root `Dockerfile.openclaw`, `build-openclaw-local-dockerfile.sh` (local `openclaw:local` tag), and `publish-openclaw-local-dockerfile.sh` (Docker Hub `${namespace}/openclaw:${OPENCLAW_VERSION}`).

## Chain

```
tini -s -- bootstrap-officecli.sh
  → bootstrap-memory-tencentdb.sh
    → validate-gogcli.sh openclaw gateway
USER: node
```

| File | When | Role |
|---|---|---|
| `bootstrap-officecli.sh` | entrypoint | Download pinned OfficeCLI (arm64 + SHA256); then exec TencentDB bootstrap |
| `bootstrap-memory-tencentdb.sh` | first start | Install cached `@tencentdb-agent-memory` plugin from `/opt/openclaw-plugin-cache` |
| `validate-gogcli.sh` | CMD | If `GOG_KEYRING_PASSWORD_FILE` set, load into process env and exec |
| `verify-tencentdb-openclaw-patch.sh` | **image build only** | Patch verifier |
| `classify-tencentdb-openclaw-hook.cjs` | **image build only** | Acorn AST classifier (`node --expose-internals`) |

## Pins

- Base: `node:26.7.0-bookworm-slim` (not backend `python:3.14.7-slim-trixie`, not sandbox `node:26.7.0-slim`).
- gogcli **linux/arm64 only**. `build-openclaw-local-dockerfile.sh` and `publish-openclaw-local-dockerfile.sh` use **docker buildx** and exit if `DOCKER_PLATFORM` is anything else.
- Publish requires `DOCKERHUB_NAMESPACE` or `OPENCLAW_PUBLISH_IMAGE` and an existing `docker login`. It tags `openclaw:local` then pushes `${repo}:${OPENCLAW_VERSION}` (and `:latest` when `PUSH_LATEST=1`).
- OfficeCLI is **runtime** download, never baked into the Dockerfile.
- TencentDB plugin install prefers the cached `.tgz`; missing cache may fall through to npm.

## Avoid

- Do not put `GOG_KEYRING_PASSWORD` in Docker env/args; file path only.
- Do not run the TencentDB classifier/patch from runtime bootstrap.
- Do not add `AGENTS.md` under `clawsec_skill_files/<skill>/` - those trees are AGPL payloads seeded into workspaces.
- Tests: `tests/test_openclaw_*.py`, `tests/test_openclaw_routing.py`. Docs: `docs/gogcli-*.md`.
- Guest LLM is whatever `openclaw.json` names. Engine writes primary + registered secondary/fallback at container start, then `openclaw_routing` rewrites the selected primary per queued turn. The gateway watches that file (hot reload). Do not add a second classifier inside the image.
