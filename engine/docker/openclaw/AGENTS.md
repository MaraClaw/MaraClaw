# docker/openclaw

Guest **OpenClaw agent image** helpers. Not the FastAPI API. Image contract also lives in repo-root `Dockerfile.openclaw` and `build-openclaw-local-dockerfile.sh`.

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

- Base: `node:26.5.0-bookworm-slim` (not backend `python:3.14.6-slim-trixie`, not sandbox `node:26.5.0-slim`).
- gogcli **linux/arm64 only**. `build-openclaw-local-dockerfile.sh` uses **docker buildx** and exits if `DOCKER_PLATFORM` is anything else.
- OfficeCLI is **runtime** download, never baked into the Dockerfile.
- TencentDB plugin install prefers the cached `.tgz`; missing cache may fall through to npm.

## Avoid

- Do not put `GOG_KEYRING_PASSWORD` in Docker env/args; file path only.
- Do not run the TencentDB classifier/patch from runtime bootstrap.
- Do not add `AGENTS.md` under `clawsec_skill_files/<skill>/` - those trees are AGPL payloads seeded into workspaces.
- Tests: `tests/test_openclaw_*.py`. Docs: `docs/gogcli-*.md`.
