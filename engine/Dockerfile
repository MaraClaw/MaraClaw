# ─── Backend Dockerfile (Optimized) ─────────────────────
# Multi-stage: deps → production
FROM python:3.14.7-slim-trixie AS deps

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

COPY pyproject.toml ./
ARG MARACLAW_PIP_INDEX_URL
ARG MARACLAW_PIP_TRUSTED_HOST
RUN if [ -n "$MARACLAW_PIP_INDEX_URL" ] && [ -n "$MARACLAW_PIP_TRUSTED_HOST" ]; then \
    pip install --no-cache-dir --index-url "$MARACLAW_PIP_INDEX_URL" --trusted-host "$MARACLAW_PIP_TRUSTED_HOST" . uv; \
    elif [ -n "$MARACLAW_PIP_INDEX_URL" ]; then \
    pip install --no-cache-dir --index-url "$MARACLAW_PIP_INDEX_URL" . uv; \
    else \
    pip install --no-cache-dir . uv; \
    fi

# ─── Production ─────────────────────────────────────────
FROM python:3.14.7-slim-trixie AS production

ARG APT_MIRROR=deb.debian.org
ARG TINI_VERSION=0.19.0-3+b7
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
        curl=8.14.1-2+deb13u4 \
        shadowsocks-libev=3.3.5+ds-16 \
        gosu=1.17-3+b4 \
        bubblewrap=0.11.0-2+deb13u1 \
        libcairo2=1.18.4-1+b1 \
        libpango-1.0-0=1.56.3-1 \
        libpangocairo-1.0-0=1.56.3-1 \
        libgdk-pixbuf-2.0-0=2.42.12+dfsg-4+deb13u1 \
        shared-mime-info=2.4-5+b2 \
        pango1.0-tools=1.56.3-1 \
        fonts-wqy-microhei=0.2.0-beta-4 \
        fonts-wqy-zenhei=0.9.45-8 \
        fonts-noto-cjk=1:20240730+repack1-1 \
        chromium=151.0.7922.137-1~deb13u1 && \
    fc-cache -f && \
    rm -rf /var/lib/apt/lists/*

# Create user before COPY so --chown resolves correctly
RUN useradd --create-home maraclaw && \
    mkdir -p /data/agents && \
    chown -R maraclaw:maraclaw /data

# Copy installed packages from deps stage
COPY --from=deps /usr/local/lib/python3.14/site-packages/ /usr/local/lib/python3.14/site-packages/
COPY --from=deps /usr/local/bin/ /usr/local/bin/

# Copy application code
COPY --chown=maraclaw:maraclaw . .

# SetUID on bwrap for hosts without unprivileged user namespaces (older kernels,
# user.max_user_namespaces=0). Guest image is Debian 13, but the *host kernel*
# still governs namespace support. Pair with --unshare-user-try in code.
# Incompatible with container --security-opt no-new-privileges and Kubernetes
# allowPrivilegeEscalation=false (those disable setuid elevation).
# Build with --build-arg BWRAP_SETUID=0 to skip on modern userns-capable hosts.
ARG BWRAP_SETUID=1
RUN chmod +x /app/entrypoint.sh && \
    BWRAP_BIN="$(command -v bwrap)" && \
    test -n "${BWRAP_BIN}" && \
    if [ "${BWRAP_SETUID}" = "1" ]; then chmod u+s "${BWRAP_BIN}"; fi

# Note: USER is removed to allow entrypoint.sh to fix permissions of mounted volumes
# at runtime. The entrypoint script will drop privileges to 'maraclaw' after fixing permissions.

# Health check
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8000/api/health || exit 1

EXPOSE 8000
# entrypoint.sh runs `schema bootstrap (bootstrap_db)` then `uvicorn`
ENTRYPOINT ["tini", "-s", "--", "/bin/bash", "/app/entrypoint.sh"]
