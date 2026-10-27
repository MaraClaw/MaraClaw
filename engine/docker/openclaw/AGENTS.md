# docker/openclaw

Guest **OpenClaw agent image** helpers. Not the FastAPI API. Image contract also lives in repo-root `Dockerfile.openclaw`, `build-openclaw-local-dockerfile.sh` (local `openclaw:local` tag), and `publish-openclaw-local-dockerfile.sh` (Docker Hub `${namespace}/openclaw:${OPENCLAW_VERSION}`).

## Chain

```
tini -s -- bootstrap-officecli.sh
  → validate-gogcli.sh openclaw gateway
USER: node
```

| File | When | Role |
|---|---|---|
| `bootstrap-officecli.sh` | entrypoint | Download pinned OfficeCLI for the running amd64/arm64 target and verify SHA256; if that fails with no valid same-architecture release, continue without OfficeCLI; exec the supplied command unchanged |
| `validate-gogcli.sh` | CMD | If `GOG_KEYRING_PASSWORD_FILE` set, load into process env and exec |

## Pins

- Base: `node:26.12.1-bookworm-slim`. Backend image is `python:3.14.8-slim-trixie`. Sandbox Node image is `node:26.12.1-slim`.
- gogcli supports **linux/amd64 and linux/arm64**, each with its own pinned digest. `build-openclaw-local-dockerfile.sh` uses **docker buildx --load** for one `DOCKER_PLATFORM` (default `linux/arm64`); other targets fail explicitly.
- Publish requires `DOCKERHUB_NAMESPACE` or `OPENCLAW_PUBLISH_IMAGE` and an existing `docker login`. It builds both platforms with **docker buildx --push** under `${repo}:${IMAGE_TAG}` (default `OPENCLAW_VERSION`), adding `:latest` only when `PUSH_LATEST=1`. It does not load or retag a local image.
- OfficeCLI is **runtime** download, never baked into the Dockerfile.
- OpenClaw is installed from its checksum-verified pinned npm archive without local memory-provider patches.

## Avoid

- Do not put `GOG_KEYRING_PASSWORD` in Docker env/args; file path only.
- Do not add `AGENTS.md` under `clawsec_skill_files/<skill>/` - those trees are AGPL payloads seeded into workspaces.
- Tests: `tests/test_openclaw_*.py`, `tests/test_openclaw_routing.py`. Docs: `docs/gogcli-*.md`.
- Guest LLM is whatever `openclaw.json` names. Engine writes primary + registered secondary/fallback at container start, then `openclaw_routing` rewrites the selected primary per queued turn. The gateway watches that file (hot reload). Do not add a second classifier inside the image.
