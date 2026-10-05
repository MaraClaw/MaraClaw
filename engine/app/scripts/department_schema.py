"""Repeatable tenant-local department schema upgrade."""

DEPARTMENT_SCHEMA = """
CREATE UNIQUE INDEX IF NOT EXISTS ux_users_id_tenant ON users (id, tenant_id);
CREATE TABLE IF NOT EXISTS local_departments (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name VARCHAR(100) NOT NULL CHECK (name = btrim(name) AND length(name) > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (id, tenant_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_local_departments_tenant_name
    ON local_departments (tenant_id, lower(name));
CREATE TABLE IF NOT EXISTS local_department_memberships (
    user_id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    department_id UUID NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (user_id, tenant_id) REFERENCES users(id, tenant_id) ON DELETE CASCADE,
    FOREIGN KEY (department_id, tenant_id) REFERENCES local_departments(id, tenant_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_local_department_memberships_department
    ON local_department_memberships (tenant_id, department_id);
DO $$ BEGIN
    ALTER TABLE agent_permissions ADD CONSTRAINT ck_department_permission_use
        CHECK (scope_type <> 'department' OR (scope_id IS NOT NULL AND access_level = 'use'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
CREATE UNIQUE INDEX IF NOT EXISTS ux_agent_permissions_department
    ON agent_permissions (agent_id, scope_id) WHERE scope_type = 'department';
"""
