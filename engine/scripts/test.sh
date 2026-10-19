#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

uv run --extra dev pytest \
  --cov=app.api.admin \
  --cov=app.api.users \
  --cov=app.api.tenants \
  --cov=app.services.admin_provisioning \
  --cov=app.services.admin_audit \
  --cov=app.services.tenant_provisioning \
  --cov=app.services.tenant_lifecycle \
  --cov=app.services.platform_admin_seeder \
  --cov=app.dao.admin_audit_dao \
  --cov=app.core.security \
  --cov-report=term-missing:skip-covered \
  --cov-fail-under=90
