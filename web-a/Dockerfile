# syntax=docker/dockerfile:1.28

# ─── MaraClaw admin console (web-a) ─────────────────────────────────────────
# Multi-stage production image:
#   1) deps    - install lockfile deps on Node 26.13.1 (cache-friendly layer)
#   2) build   - typecheck + Vite production bundle
#   3) runtime - unprivileged Caddy serves dist and proxies /api/ to engine
#
# Build:
#   docker build -t maraclaw-web-a .
#   docker build -t maraclaw-web-a --build-arg VITE_API_BASE_URL=https://api.example.com .
#
# Run:
#   Join the network where the engine runs with alias maraclaw-engine (start order does not matter).
#   docker run --rm --network maraclaw_network -p 8080:8080 maraclaw-web-a
#
# Prefer leaving VITE_API_BASE_URL empty: Caddy proxies same-origin /api/
# to http://maraclaw-engine:8000 without stripping /api from the request URI.

ARG NODE_VERSION=26.13.1

# ─── Dependencies ───────────────────────────────────────────────────────────
FROM node:${NODE_VERSION}-trixie-slim AS deps

WORKDIR /app

# Install by lockfile only - reproducible builds, maximizes layer cache.
COPY package.json package-lock.json ./
RUN npm install -g npm@12.2.2 && npm ci --no-audit --no-fund \
  && npm cache clean --force

# ─── Build ──────────────────────────────────────────────────────────────────
FROM node:${NODE_VERSION}-trixie-slim AS build

WORKDIR /app

ENV NODE_ENV=production

# Public Vite env baked into the client bundle at build time.
# Do not pass secrets here - only public VITE_* values.
ARG VITE_API_BASE_URL=
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}

# Auth bypass is always off in production builds (import.meta.env.DEV).
# Do not set VITE_AUTH_BYPASS in this stage.

COPY --from=deps /app/node_modules ./node_modules
COPY package.json package-lock.json ./
COPY index.html ./
COPY public ./public
COPY tsconfig.json tsconfig.app.json tsconfig.node.json ./
COPY vite.config.ts components.json ./
COPY src ./src

# Fail the image if TypeScript or Vite fails; assert the SPA entry exists.
RUN npm run build \
  && test -f dist/index.html

# ─── Runtime ────────────────────────────────────────────────────────────────
# Caddy serves dist on 8080 as an unprivileged user (no capabilities, no TLS, no admin API).
FROM caddy:2.11.9-alpine AS runtime

LABEL org.opencontainers.image.title="MaraClaw web-a" \
      org.opencontainers.image.description="MaraClaw admin / operator console" \
      org.opencontainers.image.vendor="MaraClaw"

# Unprivileged service account (uid/gid 101). Caddy only needs its data/config directories writable.
RUN addgroup -S -g 101 caddy \
  && adduser -S -D -H -u 101 -G caddy -s /sbin/nologin caddy \
  && chown caddy:caddy /data /config

# Replace the default site with our SPA + API proxy config (port 8080, security headers); fail the image on a bad config.
COPY docker/Caddyfile /etc/caddy/Caddyfile
RUN caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile

# Static assets only - no Node runtime, no package tree, no source.
COPY --from=build /app/dist /srv

USER caddy

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD wget -qO- http://127.0.0.1:8080/healthz >/dev/null || exit 1

CMD ["caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]
