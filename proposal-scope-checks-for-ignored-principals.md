# Proposal 4: Enforce caller scope in routes that ignore the principal

## Coding Prompt

Several engine routes authenticate the caller and then throw the principal away with `_ = current_user`. In `app/api/triggers.py`, any signed-in member can list another agent's triggers, webhook tokens and HMAC secrets included, and can rewrite or delete them. `PATCH /api/org/users/{id}` lets any org admin change the email, username, or phone of any user in any tenant, platform admins included, and forgot-password looks identities up by email. Org admins can also read other tenants' users and audit logs by passing `tenant_id`, and can delete builtin agent templates.

Close these gaps on the server. Trigger routes need agent access, and changes need manage. Use-level callers never see `token` or `secret`, and PATCH can't overwrite them. Org admins stay inside their own tenant on the org, audit-log, and template routes, and can't edit platform admin or genesis identities. Platform admin behavior doesn't change.

web-e's Tasks tab and directory page must keep working. Add handler-level tests for each gap that cover cross-tenant access, use versus manage, and the admin cases.

## How I Would Use This Codebase

I host MaraClaw for several client companies on one platform, and each client gets its own org admin. One tenant must never read another's audit trail or repoint the platform admin's email, so I'd run this change before onboarding the second client.

## Why This Is Challenging

Each gap needs its own scope rule and there is no shared choke point, so a model that adds one dependency or patches only `triggers.py` leaves the rest open. Models also tend to filter results in the client, or break web-e's directory, which calls `GET /api/org/users` without a tenant. The trigger tool already treats webhook tokens as server-owned; missing that makes PATCH accept new tokens.

## Evaluation Rubric

1. `engine/app/api/triggers.py` calls `check_agent_access` on list, PATCH, and DELETE, requires manage access (or the org and platform admin allowance that `_require_manage` in `engine/app/api/pages.py` applies) for PATCH and DELETE, and returns 403 or 404 for agents the caller cannot reach. Responses to use-level callers omit `config.token` and `config.secret`, and PATCH keeps the stored `token` and `secret` the way the update path in `engine/app/services/agent_tool_exec/_agent_tool_exec_triggers.py` keeps the old token.
2. `engine/app/api/organization.py` confines an org admin to its own tenant on `GET /org/users`, where a foreign `tenant_id` is refused or ignored, and on `PATCH /org/users/{id}`, and refuses identity email, username, or phone changes for platform admin and genesis users. `engine/app/api/enterprise.py` applies the own-tenant rule that `_get_enterprise_stats` already enforces to `GET /enterprise/audit-logs`, and `engine/app/api/advanced.py` lets only a platform admin delete builtin templates while an org admin may delete only non-builtin templates created by a user in its tenant.
3. Platform admin access is unchanged on every touched route, the checks run on the server rather than by filtering in a client, and no route is removed. web-e's `listTriggers` and `patchTrigger` calls from the Tasks tab and its `GET /api/org/users` call without `tenant_id` from the directory keep their response shapes for authorized callers.
4. Handler-direct tests with fake DAOs cover cross-tenant org admins on each route, use versus manage on triggers, secret redaction, the token-preserving PATCH, platform admin and genesis identity edits, and builtin template deletion, in `engine/tests/test_enterprise_final_boundaries.py` and new focused modules under `engine/tests/`. From `engine/`, `uv run --extra dev ruff check .`, `uv run --extra dev ruff format --check .`, `uv run --extra dev basedpyright --project pyrightconfig.json app`, `uv run --extra dev ty check --force-exclude .`, and `bash scripts/test.sh` (90 percent coverage gate) pass.
