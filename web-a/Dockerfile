# syntax=docker/dockerfile:1.7

# ─── MaraClaw admin console (web-a) ─────────────────────────────────────────
# Multi-stage production image:
#   1) deps    - install lockfile deps on Node 26 (cache-friendly layer)
#   2) build   - typecheck + Vite production bundle
#   3) runtime - unprivileged nginx serves static dist
#
# Build:
#   docker build -t maraclaw-web-a .
#   docker build -t maraclaw-web-a --build-arg VITE_API_BASE_URL=https://api.example.com .
#
# Run:
#   docker run --rm -p 8080:8080 maraclaw-web-a
#
# Prefer leaving VITE_API_BASE_URL empty and reverse-proxying /api → engine
# on the same origin so the browser never needs a cross-origin API host.

ARG NODE_VERSION=26.7.0

# ─── Dependencies ───────────────────────────────────────────────────────────
FROM node:${NODE_VERSION}-alpine AS deps

WORKDIR /app

# glibc compatibility for some optional native optionalDeps
RUN apk add --no-cache libc6-compat

# Install by lockfile only - reproducible builds, maximizes layer cache.
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund \
  && npm cache clean --force

# ─── Build ──────────────────────────────────────────────────────────────────
FROM node:${NODE_VERSION}-alpine AS build

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
# nginx-unprivileged: non-root by default, listens on 8080.
FROM nginxinc/nginx-unprivileged:1.30.4-alpine AS runtime

LABEL org.opencontainers.image.title="MaraClaw web-a" \
      org.opencontainers.image.description="MaraClaw admin / operator console" \
      org.opencontainers.image.vendor="MaraClaw"

# Replace the default site with our SPA config (port 8080, security headers).
COPY --chown=nginx:nginx docker/nginx.conf /etc/nginx/conf.d/default.conf

# Static assets only - no Node runtime, no package tree, no source.
COPY --from=build --chown=nginx:nginx /app/dist /usr/share/nginx/html

# Image already runs as uid 101 (nginx). Do not escalate.
USER nginx

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD wget -qO- http://127.0.0.1:8080/healthz >/dev/null || exit 1

STOPSIGNAL SIGQUIT

CMD ["nginx", "-g", "daemon off;"]
