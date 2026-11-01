# app/core/domain

Canonical closed engine values, grouped by owning domain. Parent: `../AGENTS.md`.

Ten leaf modules plus a docstring-only `__init__.py` (11 `.py` files). They import only the standard library: no records, DAOs, schemas, settings or I/O. Keep them that way so `app.records`, `app.core`, `app.schemas`, `app.dao` and `scripts/` can all import them without cycles.

## Modules

| File | Exports | Stored or wire mirror |
|---|---|---|
| `users.py` | `UserRole` | `user_role_enum` |
| `agents.py` | `AgentStatus`, `AgentType`, `PermissionAccessLevel`, `AgentAccessReason`, `AgentAccessDecision`, `CompanyAccessLevel`, `AgentSharingMode`, `PermissionGrantScope`, `AgentPermissionScope` | `agent_status_enum`, `permission_scope_enum`; `agent_type`, `access_mode`, `company_access_level`, `access_level` are `VARCHAR` |
| `channels.py` | `ChannelType` | `channel_type_enum` |
| `activity.py` | `ActivityAction` | `activity_action_enum` |
| `approvals.py` | `ApprovalAction`, `ApprovalStatus` | `approval_status_enum` |
| `tasks.py` | `TaskType`, `TaskStatus`, `TaskPriority` | `task_type_enum`, `task_status_enum`, `task_priority_enum` |
| `triggers.py` | `TriggerExecutionStatus` | `trigger_executions.status` |
| `focus.py` | `FocusStatus`, `FocusKind` | `agent_focus_items.status` / `kind` |
| `okr.py` | `PeriodFrequency`, `KeyResultStatus` | OKR period and key-result rows |
| `control.py` | `ControlEnvironment`, `ControlStatus` | Take Control wire results in `api/agentbay_control.py`; not stored |

## Shared and easy to confuse

- `UserRole` is the one role vocabulary. Records (`UserRecord.role`), `permissions.py`, the access and session caches, `UserDAO` role queries and several schema families all import it; do not redeclare role strings or a second role enum in a layer. `security.ROLE_HIERARCHY` repeats the same four role strings in privilege order, so keep them aligned.
- `AgentPermissionScope` is a legacy HTTP sharing selector (`company` / `user` / `custom`, where `user` means private). `AgentSharingMode` (`company` / `private` / `custom`) is the stored `access_mode`. `PermissionGrantScope` (`company` / `department` / `user`) is the permission-row grant scope. Convert at the boundary; never substitute one for another.
- `CompanyAccessLevel` is `PermissionAccessLevel | ""`. The empty string is the sealed-transfer sentinel that means no company access; do not collapse it to `use` or `manage`.
- `ChannelType` is the persisted `channel_configs.channel_type`. The provider registry (`services/channels/types.py` `ChannelKind`) wraps it with an `outbound_key` (for example `teams` for `microsoft_teams`), aliases and transport flags; those are not enum values. `atlassian` and `agentbay` are stored types too, but are `skill_only` channels.
- `ActivityAction` is a closed set, distinct from open-ended audit action names (`audit_logger`, `admin_audit`).
- `FocusKind` is closed, but focus `source` stays a free string by design.
- `AgentType` has one member (`openclaw`); the column defaults to the same value.

## Changing a value

- A new or renamed member mirrors into every place in the table: the Postgres `CREATE TYPE` (or `CHECK`/default) in the fresh-only `scripts/schema_baseline.sql`, every `Enum(str_from_row(...))` decode in `records/`, and the routes and schemas that type it. Editing the baseline does not alter an existing database, and no upgrade patch is provided.
- `scripts/api_contract_registry.py` `ENUM_TYPES` registers every enum here except `PeriodFrequency`. Adding a member changes `contracts/api-contract.json` and the generated frontend validators; refresh and check them as described in `contracts/README.md`.
- Decoding records with `Enum(value)` raises `ValueError` on an unknown stored value. Treat a rejected row as a schema/baseline mismatch, not something to coerce.
- A new channel: add `ChannelType` and `channel_type_enum` together, then follow the registry steps in `services/channels/AGENTS.md`.

## Tests

- No test file targets this package directly. `tests/test_api_contract_export.py` asserts each registered enum's exported values equal its members; record-decoding tests cover the rest. After a value change run those first, then the owning area's tests.
