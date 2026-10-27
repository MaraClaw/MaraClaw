# ─── Backend Dockerfile (Optimized) ─────────────────────
# Multi-stage: deps → production
# Linux docker CLI for pywhales (host binaries are the wrong OS).
FROM docker:29.9.2-cli AS dockercli

FROM python:3.14.8-slim-trixie AS deps

ARG APT_MIRROR=deb.debian.org
WORKDIR /app
RUN if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
    sed -i "s/deb.debian.org/${APT_MIRROR}/g" /etc/apt/sources.list.d/debian.sources; \
    else \
    sed -i "s/deb.debian.org/${APT_MIRROR}/g" /etc/apt/sources.list; \
    fi && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        gcc=4:14.2.0-1 \
        libpq-dev=17.11-0+deb13u1 \
        libcairo2=1.18.4-1+b1 \
        libpango-1.0-0=1.56.3-1 \
        libpangocairo-1.0-0=1.56.3-1 \
        libgdk-pixbuf-2.0-0=2.42.12+dfsg-4+deb13u1 \
        shared-mime-info=2.4-5+b2 \
        pango1.0-tools=1.56.3-1 && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
ARG MARACLAW_PIP_INDEX_URL
ARG MARACLAW_PIP_TRUSTED_HOST
RUN set -eu; set --; \
    if [ -n "${MARACLAW_PIP_INDEX_URL:-}" ]; then set -- "$@" --index-url "$MARACLAW_PIP_INDEX_URL"; fi; \
    if [ -n "${MARACLAW_PIP_TRUSTED_HOST:-}" ]; then set -- "$@" --trusted-host "$MARACLAW_PIP_TRUSTED_HOST"; fi; \
    pip install --no-cache-dir "$@" uv==0.13.5 && \
    uv export --locked --no-dev --no-emit-project --format requirements.txt --output-file /tmp/requirements.txt && \
    uv pip install --system --no-cache --require-hashes "$@" -r /tmp/requirements.txt && \
    rm -f /tmp/requirements.txt

# ─── Production ─────────────────────────────────────────
FROM python:3.14.8-slim-trixie AS production

ARG APT_MIRROR=deb.debian.org
ARG TINI_VERSION=0.19.0-3+b8
WORKDIR /app
RUN if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
    sed -i "s/deb.debian.org/${APT_MIRROR}/g" /etc/apt/sources.list.d/debian.sources; \
    else \
    sed -i "s/deb.debian.org/${APT_MIRROR}/g" /etc/apt/sources.list; \
    fi && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        "tini=${TINI_VERSION}" \
        libpq5=17.11-0+deb13u1 \
        curl=8.14.1-2+deb13u7 \
        shadowsocks-libev=3.3.5+ds-16 \
        gosu=1.17-3+b4 \
        bubblewrap=0.12.0-1~deb13u1 \
        libcairo2=1.18.4-1+b1 \
        libpango-1.0-0=1.56.3-1 \
        libpangocairo-1.0-0=1.56.3-1 \
        libgdk-pixbuf-2.0-0=2.42.12+dfsg-4+deb13u1 \
        shared-mime-info=2.4-5+b2 \
        pango1.0-tools=1.56.3-1 \
        fonts-wqy-microhei=0.2.0-beta-4 \
        fonts-wqy-zenhei=0.9.45-8 \
        fonts-noto-cjk=1:20240730+repack1-1 \
        chromium=156.0.8078.47-1~deb13u1 && \
    fc-cache -f && \
    rm -rf /var/lib/apt/lists/*

# Create user before COPY so --chown resolves correctly
RUN useradd --create-home maraclaw && \
    mkdir -p /data/agents && \
    chown -R maraclaw:maraclaw /data

# Copy installed packages from deps stage
COPY --from=deps /usr/local/lib/python3.14/site-packages/ /usr/local/lib/python3.14/site-packages/
COPY --from=deps /usr/local/bin/ /usr/local/bin/
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker

# Copy application code
COPY --chown=maraclaw:maraclaw . .

# Prove firecrawl-anydoc imports and converts in this image (not host-only).
RUN python /app/scripts/verify_anydoc.py

# Bubblewrap 0.12 rejects setuid execution. Keep it unprivileged even if an
# upstream package supplies the bit; non-root sandboxing requires host userns.
RUN chmod +x /app/entrypoint.sh && \
    BWRAP_BIN="$(command -v bwrap)" && \
    test -n "${BWRAP_BIN}" && \
    chmod u-s "${BWRAP_BIN}"

# Note: USER is removed to allow entrypoint.sh to fix permissions of mounted volumes
# at runtime. The entrypoint script will drop privileges to 'maraclaw' after fixing permissions.

# Health check
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8000/api/health || exit 1

EXPOSE 8000
# entrypoint.sh runs `schema bootstrap (bootstrap_db)` then `uvicorn`
ENTRYPOINT ["tini", "-s", "--", "/bin/bash", "/app/entrypoint.sh"]
