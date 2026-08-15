"""OKR REST API - objectives, key results, settings, reports and periods.

All endpoints are tenant-scoped: data is filtered by the requesting user's
tenant_id so cross-tenant leakage is impossible.

Route summary
─────────────
GET/PUT   /api/okr/settings
GET       /api/okr/periods
GET/POST  /api/okr/objectives
PATCH     /api/okr/objectives/{id}
GET/POST  /api/okr/objectives/{id}/key-results
PATCH     /api/okr/key-results/{id}
POST      /api/okr/key-results/{id}/progress        (manual progress update)
GET       /api/okr/reports
GET       /api/okr/members-without-okr             (P4 onboarding: admin view)
POST      /api/okr/trigger-member-outreach         (P4 onboarding: fire OKR Agent)
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any, NotRequired, TypedDict

from fastapi import APIRouter, Depends, HTTPException

from app.api.auth import get_current_user
from app.core.json_types import JsonObject, object_mapping_from, str_from_row, uuid_from_row
from app.core.logging import logger
from app.dao.agent_agent_relationship_dao import agent_agent_relationship_dao
from app.dao.agent_dao import agent_dao
from app.dao.agent_relationship_dao import agent_relationship_dao
from app.dao.channel_config_dao import channel_config_dao
from app.dao.chat_dao import chat_message_dao, chat_session_dao
from app.dao.notification_dao import notification_dao
from app.dao.okr_dao import (
    okr_key_result_dao,
    okr_objective_dao,
    okr_progress_log_dao,
    work_report_dao,
)
from app.dao.okr_settings_dao import okr_settings_dao
from app.dao.org_member_dao import org_member_dao
from app.dao.trigger_dao import agent_trigger_dao
from app.dao.user_dao import user_dao
from app.records.okr import (
    CompanyReportRecord,
    OKRKeyResultRecord,
    OKRObjectiveRecord,
    OKRSettingsRecord,
)
from app.records.user import UserRecord
from app.schemas.okr import (
    CompanyReportOut,
    CompanyReportRegenerate,
    KeyResultCreate,
    KeyResultOut,
    KeyResultUpdate,
    MemberDailyReportOut,
    MemberDailyReportUpsert,
    ObjectiveCreate,
    ObjectiveOut,
    ObjectiveUpdate,
    OKRSettingsOut,
    OKRSettingsUpdate,
    PeriodOut,
    ProgressUpdate,
    WorkReportOut,
)
from app.services.okr_periods import advance_period, compute_current_period, compute_period_for_date

router = APIRouter(prefix="/api/okr", tags=["okr"])


class MemberWithoutOkr(TypedDict):
    id: str
    type: str
    display_name: str
    avatar_url: str
    channel: str | None
    channel_user_id: str | None
    source_label: NotRequired[str | None]


class ChannelWarning(TypedDict):
    channel_type: str
    channel_display: str
    affected_members: list[str]
    count: int


type TrackedMember = tuple[uuid.UUID, str, uuid.UUID | None, str | None, str | None, str | None]


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _is_okr_admin(user: UserRecord) -> bool:
    return getattr(user, "role", None) in ("org_admin", "platform_admin")


def _dashboard_write_forbidden() -> HTTPException:
    return HTTPException(
        403,
        "Only org admins can modify OKRs in the dashboard. Members should use OKR Agent to manage their own OKRs.",
    )


def _require_tenant_id(user: UserRecord) -> uuid.UUID:
    tenant_id = user.tenant_id
    if tenant_id is None:
        raise HTTPException(status_code=400, detail="Tenant context required")
    return tenant_id


async def _sync_okr_agent_relationships(tenant_id: uuid.UUID, okr_agent_id: uuid.UUID) -> None:
    """Auto-connect the OKR Agent to all active org members and company-visible agents."""
    _ = await agent_relationship_dao.delete_for_agent(okr_agent_id)
    _ = await agent_agent_relationship_dao.delete_for_agent(okr_agent_id)

    for member_id in await org_member_dao.list_active_ids_for_tenant(tenant_id):
        _ = await agent_relationship_dao.create(
            obj_in={
                "agent_id": okr_agent_id,
                "member_id": member_id,
                "relation": "team_member",
                "description": "OKR tracking - auto-linked via Sync Relationships",
            }
        )

    for agent in await agent_dao.list_active_nonsystem_for_tenant(
        tenant_id, exclude_id=okr_agent_id, company_only=True
    ):
        _ = await agent_agent_relationship_dao.create(
            obj_in={
                "agent_id": okr_agent_id,
                "target_agent_id": agent.id,
                "relation": "collaborator",
            }
        )

    try:
        from app.api.relationships import _regenerate_relationships_file

        await _regenerate_relationships_file(okr_agent_id)
    except Exception as error:
        logger.warning(f"[OKR] Failed to regenerate relationships file: {error}")


async def _get_or_create_settings(tenant_id: uuid.UUID) -> OKRSettingsRecord:
    """Return the OKRSettings row for this tenant, creating it if missing."""
    return await okr_settings_dao.get_or_create(tenant_id)


async def _sync_okr_report_triggers(settings: OKRSettingsRecord) -> None:
    """Keep OKR Agent system triggers aligned with tenant report settings."""
    if not settings.okr_agent_id:
        return

    from app.services.focus_service import ensure_focus_item

    system_focus_ref = await ensure_focus_item(
        settings.okr_agent_id,
        focus_ref="system:okr_reports",
        description="Automated OKR aggregation, daily report collection, and periodic reports",
        system=True,
    )

    daily_hour, daily_minute = 18, 0
    try:
        daily_hour_str, daily_minute_str = settings.daily_report_time.split(":", 1)
        daily_hour = max(0, min(23, int(daily_hour_str)))
        daily_minute = max(0, min(59, int(daily_minute_str)))
    except Exception:
        logger.warning(f"[OKR] Invalid daily_report_time {settings.daily_report_time}; using 18:00")

    all_triggers = await agent_trigger_dao.list_for_agent(settings.okr_agent_id)
    names = {
        "daily_okr_collection",
        "daily_okr_report",
        "weekly_okr_report",
        "biweekly_okr_checkin",
        "monthly_okr_report",
    }
    triggers = {t.name: t for t in all_triggers if t.name in names}

    async def _ensure_trigger(name: str, *, config: JsonObject, reason: str, is_enabled: bool) -> None:
        trigger = triggers.get(name)
        if trigger is None:
            created = await agent_trigger_dao.create(
                obj_in={
                    "agent_id": settings.okr_agent_id,
                    "name": name,
                    "type": "cron",
                    "config": config,
                    "reason": reason,
                    "cooldown_seconds": 3600,
                    "is_system": True,
                    "focus_ref": system_focus_ref,
                    "is_enabled": is_enabled,
                }
            )
            triggers[name] = created
            return
        _ = await agent_trigger_dao.update(
            db_obj=trigger,
            obj_in={
                "config": config,
                "reason": reason,
                "is_enabled": is_enabled,
                "focus_ref": trigger.focus_ref or system_focus_ref,
            },
        )

    await _ensure_trigger(
        "daily_okr_collection",
        config={"expr": f"{daily_minute} {daily_hour} * * *"},
        is_enabled=bool(settings.enabled and settings.daily_report_enabled),
        reason=(
            "System trigger: daily OKR collection. When daily reporting is enabled, "
            + "the OKR Agent should collect today's final daily update only from members "
            + "and agents already in its relationship list."
        ),
    )
    await _ensure_trigger(
        "daily_okr_report",
        config={"expr": "0 9 * * *"},
        is_enabled=bool(settings.enabled),
        reason=("System trigger: generate the company daily report at 09:00 for the previous day."),
    )
    await _ensure_trigger(
        "weekly_okr_report",
        config={"expr": "0 9 * * 1"},
        is_enabled=bool(settings.enabled),
        reason=("System trigger: generate the company weekly report at 09:00 every Monday for the previous week."),
    )
    biweekly = triggers.get("biweekly_okr_checkin")
    if biweekly:
        _ = await agent_trigger_dao.update(
            db_obj=biweekly,
            obj_in={
                "is_enabled": bool(settings.enabled),
                "reason": (
                    "System trigger: fires on the 1st and 15th of every month at 10:00 "
                    + "to perform the mandatory bi-weekly OKR check-in."
                ),
            },
        )
    await _ensure_trigger(
        "monthly_okr_report",
        config={"expr": "0 9 1 * *"},
        is_enabled=bool(settings.enabled),
        reason=("System trigger: generate the company monthly report at 09:00 on the 1st for the previous month."),
    )


_compute_current_period = compute_current_period
_compute_period_for_date = compute_period_for_date
_advance_period = advance_period


# ─── Settings ─────────────────────────────────────────────────────────────────


@router.get("/settings", response_model=OKRSettingsOut)
async def get_okr_settings(user: UserRecord = Depends(get_current_user)):
    """Return OKR configuration for the current tenant."""
    settings = await _get_or_create_settings(_require_tenant_id(user))
    okr_agent_id_str = str(settings.okr_agent_id) if settings.okr_agent_id else None
    return OKRSettingsOut(
        enabled=settings.enabled,
        first_enabled_at=settings.first_enabled_at.isoformat() if settings.first_enabled_at else None,
        daily_report_enabled=settings.daily_report_enabled,
        daily_report_time=settings.daily_report_time,
        daily_report_skip_non_workdays=settings.daily_report_skip_non_workdays,
        weekly_report_enabled=False,
        weekly_report_day=0,
        period_frequency=settings.period_frequency,
        period_length_days=settings.period_length_days,
        period_frequency_locked=settings.first_enabled_at is not None,
        okr_agent_id=okr_agent_id_str,
    )


@router.put("/settings", response_model=OKRSettingsOut)
async def update_okr_settings(body: OKRSettingsUpdate, user: UserRecord = Depends(get_current_user)):
    """Update OKR configuration. Org admins only."""
    # Allow org admins and platform admins to modify OKR settings.
    # user.role is the canonical authority; is_admin is not a real field.
    if getattr(user, "role", None) not in ("org_admin", "platform_admin"):
        raise HTTPException(403, "Only org admins can modify OKR settings")

    settings = await _get_or_create_settings(_require_tenant_id(user))
    period_is_locked = settings.first_enabled_at is not None

    if period_is_locked:
        if body.period_frequency is not None and body.period_frequency != settings.period_frequency:
            raise HTTPException(
                400,
                "OKR period frequency is locked after OKR is first enabled.",
            )
        if body.period_length_days is not None and body.period_length_days != settings.period_length_days:
            raise HTTPException(
                400,
                "OKR period length is locked after OKR is first enabled.",
            )

    updates: dict[str, Any] = {
        "weekly_report_enabled": False,
        "weekly_report_day": 0,
    }
    if body.enabled is not None:
        updates["enabled"] = body.enabled
    if body.daily_report_enabled is not None:
        updates["daily_report_enabled"] = body.daily_report_enabled
    if body.daily_report_time is not None:
        updates["daily_report_time"] = body.daily_report_time
    if body.daily_report_skip_non_workdays is not None:
        updates["daily_report_skip_non_workdays"] = body.daily_report_skip_non_workdays
    if body.period_frequency is not None:
        updates["period_frequency"] = body.period_frequency
    if body.period_length_days is not None:
        updates["period_length_days"] = body.period_length_days
    if body.enabled is True and settings.first_enabled_at is None:
        updates["first_enabled_at"] = datetime.now(UTC)

    settings = await okr_settings_dao.update(db_obj=settings, obj_in=updates)
    await _sync_okr_report_triggers(settings)

    okr_agent_id_str: str | None = str(settings.okr_agent_id) if settings.okr_agent_id else None

    if body.enabled and not settings.okr_agent_id:
        from app.services.agent_seeder import seed_okr_agent_for_tenant

        logger.info(f"[OKR] OKR enabled for tenant {_require_tenant_id(user)} - auto-seeding OKR Agent")
        await seed_okr_agent_for_tenant(_require_tenant_id(user), user.id)
        refreshed = await _get_or_create_settings(_require_tenant_id(user))
        await _sync_okr_report_triggers(refreshed)
        okr_agent_id_str = str(refreshed.okr_agent_id) if refreshed.okr_agent_id else None
        settings = refreshed

    return OKRSettingsOut(
        enabled=settings.enabled,
        first_enabled_at=settings.first_enabled_at.isoformat() if settings.first_enabled_at else None,
        daily_report_enabled=settings.daily_report_enabled,
        daily_report_time=settings.daily_report_time,
        daily_report_skip_non_workdays=settings.daily_report_skip_non_workdays,
        weekly_report_enabled=False,
        weekly_report_day=0,
        period_frequency=settings.period_frequency,
        period_length_days=settings.period_length_days,
        period_frequency_locked=settings.first_enabled_at is not None,
        okr_agent_id=okr_agent_id_str,
    )


# ─── Sync Relationships ───────────────────────────────────────────────────────


@router.post("/sync-relationships")
async def sync_okr_relationships(user: UserRecord = Depends(get_current_user)):
    """Manually re-sync the OKR Agent's relationship network.

    Connects the OKR Agent to all active OrgMembers (org-structure-synced humans)
    and all company-visible agents in this tenant. Idempotent - safe to call
    multiple times; existing relationships are replaced.

    Org admins and platform admins only.
    """
    if getattr(user, "role", None) not in ("org_admin", "platform_admin"):
        raise HTTPException(403, "Only org admins can sync OKR relationships")

    settings = await _get_or_create_settings(_require_tenant_id(user))
    if not settings.okr_agent_id:
        raise HTTPException(404, "OKR Agent not found for this tenant. Enable OKR in Company Settings first.")
    okr_agent_id = settings.okr_agent_id
    await _sync_okr_agent_relationships(_require_tenant_id(user), okr_agent_id)
    return {"status": "ok", "okr_agent_id": str(okr_agent_id)}


# ─── Periods ──────────────────────────────────────────────────────────────────


@router.get("/periods", response_model=list[PeriodOut])
async def list_periods(user: UserRecord = Depends(get_current_user)):
    """Return OKR periods from first enablement through the next period.

    Periods are computed from the tenant's locked OKR cadence. Once OKR has
    been enabled for a tenant, the first enabled period remains the start of
    the selectable history even if OKR is later disabled and re-enabled.
    """
    settings = await _get_or_create_settings(_require_tenant_id(user))
    first_enabled_at = settings.first_enabled_at
    if first_enabled_at is None and settings.enabled:
        earliest_period_start = await okr_objective_dao.earliest_period_start(_require_tenant_id(user))
        if earliest_period_start:
            first_enabled_at = datetime.combine(
                earliest_period_start,
                datetime.min.time(),
                tzinfo=UTC,
            )
        else:
            first_enabled_at = datetime.now(UTC)
        settings = await okr_settings_dao.update(db_obj=settings, obj_in={"first_enabled_at": first_enabled_at})

    freq = settings.period_frequency
    length = settings.period_length_days

    def _period_label(start: date, freq: str) -> str:
        if freq == "monthly":
            return start.strftime("%b %Y")
        if freq == "quarterly":
            q = (start.month - 1) // 3 + 1
            return f"Q{q} {start.year}"
        end = start + timedelta(days=(length or 90) - 1)
        return f"{start.isoformat()} - {end.isoformat()}"

    cur_start, _ = _compute_current_period(freq, length)
    first_anchor = first_enabled_at.date() if first_enabled_at else datetime.now(UTC).date()
    start, _ = _compute_period_for_date(freq, length, first_anchor)
    final_start, _ = _advance_period(cur_start, freq, length, 1)

    all_periods: list[tuple[date, date]] = []
    cursor_start = start
    guard = 0
    while cursor_start <= final_start and guard < 600:
        period_start, period_end = _compute_period_for_date(freq, length, cursor_start)
        all_periods.append((period_start, period_end))
        cursor_start, _ = _advance_period(period_start, freq, length, 1)
        guard += 1

    return [
        PeriodOut(
            start=s.isoformat(),
            end=e.isoformat(),
            label=_period_label(s, freq),
            is_current=(s == cur_start),
        )
        for s, e in all_periods
    ]


# ─── Objectives ───────────────────────────────────────────────────────────────


def _kr_to_out(kr: OKRKeyResultRecord) -> KeyResultOut:
    return KeyResultOut(
        id=str(kr.id),
        objective_id=str(kr.objective_id),
        title=kr.title,
        target_value=kr.target_value,
        current_value=kr.current_value,
        unit=kr.unit,
        focus_ref=kr.focus_ref,
        status=kr.status,
        last_updated_at=kr.last_updated_at.isoformat() if kr.last_updated_at else "",
        created_at=kr.created_at.isoformat() if kr.created_at else "",
    )


def _obj_to_out(
    obj: OKRObjectiveRecord,
    krs: list[OKRKeyResultRecord] | None = None,
    owner_name: str | None = None,
) -> ObjectiveOut:
    return ObjectiveOut(
        id=str(obj.id),
        title=obj.title,
        description=obj.description,
        owner_type=obj.owner_type,
        owner_id=str(obj.owner_id) if obj.owner_id else None,
        owner_name=owner_name,
        period_start=obj.period_start.isoformat(),
        period_end=obj.period_end.isoformat(),
        status=obj.status,
        created_at=obj.created_at.isoformat() if obj.created_at else "",
        key_results=[_kr_to_out(kr) for kr in (krs or [])],
    )


@router.get("/objectives", response_model=list[ObjectiveOut])
async def list_objectives(
    period_start: str | None = None,
    period_end: str | None = None,
    user: UserRecord = Depends(get_current_user),
):
    """List all Objectives for the current tenant within a period.

    If period_start / period_end are not supplied, defaults to the current
    OKR period computed from the tenant's OKR settings.
    Includes owner_name resolved from User.display_name or Agent.name.
    """
    if not period_start or not period_end:
        settings = await _get_or_create_settings(_require_tenant_id(user))
        ps, pe = _compute_current_period(settings.period_frequency, settings.period_length_days)
    else:
        ps = date.fromisoformat(period_start)
        pe = date.fromisoformat(period_end)

    objectives = await okr_objective_dao.list_for_period(_require_tenant_id(user), period_start=ps, period_end=pe)
    obj_ids = [o.id for o in objectives]
    all_krs = await okr_key_result_dao.list_for_objectives(obj_ids)
    krs_by_obj: dict[uuid.UUID, list[OKRKeyResultRecord]] = {}
    for kr in all_krs:
        krs_by_obj.setdefault(kr.objective_id, []).append(kr)

    user_owner_ids = [o.owner_id for o in objectives if o.owner_type == "user" and o.owner_id]
    agent_owner_ids = [o.owner_id for o in objectives if o.owner_type == "agent" and o.owner_id]

    user_names = await user_dao.display_names_for_ids(user_owner_ids) if user_owner_ids else {}
    unresolved_ids = [oid for oid in user_owner_ids if oid not in user_names]
    if unresolved_ids:
        _ = user_names.update(await org_member_dao.names_for_ids(unresolved_ids))

    agent_names = await agent_dao.names_for_ids(agent_owner_ids) if agent_owner_ids else {}

    def _resolve_name(obj: OKRObjectiveRecord) -> str | None:
        if not obj.owner_id:
            return None
        if obj.owner_type == "user":
            return user_names.get(obj.owner_id)
        if obj.owner_type == "agent":
            return agent_names.get(obj.owner_id)
        return None

    return [_obj_to_out(o, krs_by_obj.get(o.id, []), owner_name=_resolve_name(o)) for o in objectives]


@router.post("/objectives", response_model=ObjectiveOut)
async def create_objective(body: ObjectiveCreate, user: UserRecord = Depends(get_current_user)):
    """Create a new Objective."""
    if not _is_okr_admin(user):
        raise _dashboard_write_forbidden()

    resolved_owner_id: uuid.UUID | None = None

    if body.owner_id:
        candidate = uuid.UUID(body.owner_id)

        if body.owner_type == "user":
            if await user_dao.exists(candidate):
                resolved_owner_id = candidate
            else:
                member = await org_member_dao.get(candidate)
                if member:
                    if member.user_id:
                        resolved_owner_id = member.user_id
                        logger.info(
                            f"[create_objective] Resolved OrgMember.id {candidate} → user_id {resolved_owner_id}"
                        )
                    else:
                        resolved_owner_id = candidate
                        logger.info(
                            f"[create_objective] Channel-only OrgMember {candidate} "
                            + "has no user_id - storing OrgMember.id as owner_id"
                        )
                else:
                    raise HTTPException(
                        422,
                        f"owner_id '{body.owner_id}' does not match any User or OrgMember in this tenant",
                    )
        else:
            resolved_owner_id = candidate

    obj = await okr_objective_dao.create(
        obj_in={
            "tenant_id": _require_tenant_id(user),
            "title": body.title,
            "description": body.description,
            "owner_type": body.owner_type,
            "owner_id": resolved_owner_id,
            "period_start": date.fromisoformat(body.period_start),
            "period_end": date.fromisoformat(body.period_end),
        }
    )
    return _obj_to_out(obj)


@router.patch("/objectives/{objective_id}", response_model=ObjectiveOut)
async def update_objective(
    objective_id: uuid.UUID,
    body: ObjectiveUpdate,
    user: UserRecord = Depends(get_current_user),
):
    """Update an Objective's title, description or status."""
    if not _is_okr_admin(user):
        raise _dashboard_write_forbidden()

    obj = await okr_objective_dao.get_for_tenant(objective_id, _require_tenant_id(user))
    if not obj:
        raise HTTPException(404, "Objective not found")

    updates = {}
    if body.title is not None:
        updates["title"] = body.title
    if body.description is not None:
        updates["description"] = body.description
    if body.status is not None:
        updates["status"] = body.status
    if updates:
        obj = await okr_objective_dao.update(db_obj=obj, obj_in=updates)
    return _obj_to_out(obj)


@router.delete("/objectives/{objective_id}")
async def delete_objective(
    objective_id: uuid.UUID,
    user: UserRecord = Depends(get_current_user),
):
    """Soft delete an Objective (set status to archived)."""
    if not _is_okr_admin(user):
        raise _dashboard_write_forbidden()

    obj = await okr_objective_dao.get_for_tenant(objective_id, _require_tenant_id(user))
    if not obj:
        raise HTTPException(404, "Objective not found")
    _ = await okr_objective_dao.update(db_obj=obj, obj_in={"status": "archived"})
    return {"status": "success"}


# ─── Key Results ──────────────────────────────────────────────────────────────


@router.get("/objectives/{objective_id}/key-results", response_model=list[KeyResultOut])
async def list_key_results(objective_id: uuid.UUID, user: UserRecord = Depends(get_current_user)):
    """List all KRs for the given Objective."""
    if not await okr_objective_dao.get_for_tenant(objective_id, _require_tenant_id(user)):
        raise HTTPException(404, "Objective not found")
    return [_kr_to_out(kr) for kr in await okr_key_result_dao.list_for_objective(objective_id)]


@router.post("/objectives/{objective_id}/key-results", response_model=KeyResultOut)
async def create_key_result(
    objective_id: uuid.UUID,
    body: KeyResultCreate,
    user: UserRecord = Depends(get_current_user),
):
    """Create a new Key Result under the specified Objective."""
    if not _is_okr_admin(user):
        raise _dashboard_write_forbidden()

    if not await okr_objective_dao.get_for_tenant(objective_id, _require_tenant_id(user)):
        raise HTTPException(404, "Objective not found")

    kr = await okr_key_result_dao.create(
        obj_in={
            "objective_id": objective_id,
            "title": body.title,
            "target_value": body.target_value,
            "unit": body.unit,
            "focus_ref": body.focus_ref,
        }
    )
    return _kr_to_out(kr)


@router.patch("/key-results/{kr_id}", response_model=KeyResultOut)
async def update_key_result(
    kr_id: uuid.UUID,
    body: KeyResultUpdate,
    user: UserRecord = Depends(get_current_user),
):
    """Update a Key Result's fields or current progress value.

    When current_value changes, an OKRProgressLog entry is created
    automatically to maintain the complete progress history.
    """
    if not _is_okr_admin(user):
        raise _dashboard_write_forbidden()

    row = await okr_key_result_dao.get_with_tenant(kr_id, _require_tenant_id(user))
    if not row:
        raise HTTPException(404, "Key Result not found")
    kr, _ = row
    prev_value = kr.current_value
    updates = {}
    if body.title is not None:
        updates["title"] = body.title
    if body.target_value is not None:
        updates["target_value"] = body.target_value
    if body.current_value is not None:
        updates["current_value"] = body.current_value
    if body.unit is not None:
        updates["unit"] = body.unit
    if body.focus_ref is not None:
        updates["focus_ref"] = body.focus_ref
    if body.status is not None:
        updates["status"] = body.status
    if updates:
        kr = await okr_key_result_dao.update(db_obj=kr, obj_in=updates)

    if body.current_value is not None and body.current_value != prev_value:
        _ = await okr_progress_log_dao.create(
            obj_in={
                "kr_id": kr_id,
                "previous_value": prev_value,
                "new_value": body.current_value,
                "source": "manual",
            }
        )
    return _kr_to_out(kr)


@router.post("/key-results/{kr_id}/progress", response_model=KeyResultOut)
async def update_kr_progress_endpoint(
    kr_id: uuid.UUID,
    body: ProgressUpdate,
    user: UserRecord = Depends(get_current_user),
):
    """Convenience endpoint for updating only the current progress value.

    Used by the update_kr_progress agent tool and the OKR Agent.
    Records an OKRProgressLog entry with the provided note.
    """
    if not _is_okr_admin(user):
        raise _dashboard_write_forbidden()

    row = await okr_key_result_dao.get_with_tenant(kr_id, _require_tenant_id(user))
    if not row:
        raise HTTPException(404, "Key Result not found")
    kr, _ = row
    prev_value = kr.current_value
    status = kr.status
    if body.status and body.status in ("on_track", "at_risk", "behind", "completed"):
        status = body.status
    elif kr.target_value:
        ratio = body.value / kr.target_value
        if ratio >= 1.0:
            status = "completed"
        elif ratio >= 0.7:
            status = "on_track"
        elif ratio >= 0.4:
            status = "at_risk"
        else:
            status = "behind"

    kr = await okr_key_result_dao.update(
        db_obj=kr,
        obj_in={
            "current_value": body.value,
            "status": status,
            "last_updated_at": datetime.now(UTC),
        },
    )
    _ = await okr_progress_log_dao.create(
        obj_in={
            "kr_id": kr_id,
            "previous_value": prev_value,
            "new_value": body.value,
            "source": "manual",
            "note": body.note,
        }
    )
    return _kr_to_out(kr)


@router.delete("/key-results/{kr_id}")
async def delete_key_result(
    kr_id: uuid.UUID,
    user: UserRecord = Depends(get_current_user),
):
    """Hard delete a key result."""
    if not _is_okr_admin(user):
        raise _dashboard_write_forbidden()

    row = await okr_key_result_dao.get_with_tenant(kr_id, _require_tenant_id(user))
    if not row:
        raise HTTPException(404, "Key Result not found")
    _ = await okr_progress_log_dao.delete_for_kr(kr_id)
    _ = await okr_key_result_dao.delete(id=kr_id)
    return {"status": "success"}


# ─── Reports ──────────────────────────────────────────────────────────────────


def _serialize_company_report(report: CompanyReportRecord) -> CompanyReportOut:
    return CompanyReportOut(
        id=str(report.id),
        report_type=report.report_type,
        period_start=report.period_start.isoformat(),
        period_end=report.period_end.isoformat(),
        period_label=report.period_label,
        content=report.content,
        submitted_count=report.submitted_count,
        missing_count=report.missing_count,
        needs_refresh=report.needs_refresh,
        generated_at=report.generated_at.isoformat() if report.generated_at else "",
        updated_at=report.updated_at.isoformat() if report.updated_at else "",
    )


@router.get("/member-daily-reports", response_model=list[MemberDailyReportOut])
async def list_member_daily_reports(
    report_date: str | None = None,
    user: UserRecord = Depends(get_current_user),
):
    """List all member daily reports for a specific date plus missing members."""
    from app.services.okr_reporting import list_member_daily_reports_for_date

    target_day = date.fromisoformat(report_date) if report_date else datetime.now(UTC).date()
    items = await list_member_daily_reports_for_date(_require_tenant_id(user), target_day)
    return [
        MemberDailyReportOut(
            id=f"{item['member_type']}:{item['member_id']}:{target_day.isoformat()}",
            member_type=item["member_type"],
            member_id=item["member_id"],
            display_name=item["display_name"],
            avatar_url=item["avatar_url"],
            group_label=item["group_label"],
            report_date=target_day.isoformat(),
            content=item["content"],
            status=item["status"],
            submitted_at=item["submitted_at"],
            updated_at=item["updated_at"],
        )
        for item in items
    ]


@router.post("/member-daily-reports", response_model=MemberDailyReportOut)
async def upsert_member_daily_report(
    body: MemberDailyReportUpsert,
    user: UserRecord = Depends(get_current_user),
):
    """Create or update a member daily report.

    Regular members can only edit their own user report.
    Org admins and platform admins may specify a tenant member explicitly.
    """
    from app.services.okr_reporting import (
        list_tracked_okr_members,
        upsert_member_daily_report as _upsert,
    )

    target_member_type = body.member_type or "user"
    target_member_id = uuid.UUID(body.member_id) if body.member_id else user.id

    if getattr(user, "role", None) not in ("org_admin", "platform_admin") and (
        target_member_type != "user" or target_member_id != user.id
    ):
        raise HTTPException(403, "You can only submit your own daily report")

    report_date = date.fromisoformat(body.report_date)
    report = await _upsert(
        tenant_id=_require_tenant_id(user),
        member_type=target_member_type,
        member_id=target_member_id,
        report_date=report_date,
        content=body.content,
        source=body.source,
    )
    member_map = {
        (member.member_type, str(member.member_id)): member
        for member in await list_tracked_okr_members(_require_tenant_id(user))
    }
    member_meta = member_map.get((report.member_type, str(report.member_id)))
    return MemberDailyReportOut(
        id=str(report.id),
        member_type=report.member_type,
        member_id=str(report.member_id),
        display_name=member_meta.display_name if member_meta else str(report.member_id),
        avatar_url=member_meta.avatar_url if member_meta else None,
        group_label=member_meta.group_label if member_meta else "Members",
        report_date=report.report_date.isoformat(),
        content=report.content,
        status=report.status,
        submitted_at=report.submitted_at.isoformat() if report.submitted_at else None,
        updated_at=report.updated_at.isoformat() if report.updated_at else None,
    )


@router.get("/company-reports", response_model=list[CompanyReportOut])
async def list_company_reports_api(
    report_type: str | None = None,
    limit: int = 50,
    user: UserRecord = Depends(get_current_user),
):
    """List company-level reports from the new reporting pipeline."""
    from app.services.okr_reporting import list_company_reports

    reports = await list_company_reports(_require_tenant_id(user), report_type=report_type, limit=limit)
    return [_serialize_company_report(report) for report in reports]


@router.post("/company-reports/regenerate", response_model=CompanyReportOut)
async def regenerate_company_report(
    body: CompanyReportRegenerate,
    user: UserRecord = Depends(get_current_user),
):
    """Rebuild a single company report for a target period."""
    if getattr(user, "role", None) not in ("org_admin", "platform_admin"):
        raise HTTPException(403, "Only org admins can regenerate company reports")

    from app.services.okr_reporting import (
        generate_company_daily_report,
        generate_company_monthly_report,
        generate_company_weekly_report,
    )

    period_start = date.fromisoformat(body.period_start)
    if body.report_type == "daily":
        report = await generate_company_daily_report(_require_tenant_id(user), period_start)
    elif body.report_type == "weekly":
        report = await generate_company_weekly_report(_require_tenant_id(user), period_start)
    elif body.report_type == "monthly":
        report = await generate_company_monthly_report(_require_tenant_id(user), period_start)
    else:
        raise HTTPException(400, "Invalid report_type")

    return _serialize_company_report(report)


@router.get("/reports", response_model=list[WorkReportOut])
async def list_reports(
    report_type: str | None = None,  # "daily" | "weekly" | None for both
    limit: int = 50,
    user: UserRecord = Depends(get_current_user),
):
    """List work reports for the current tenant, newest first."""
    reports = await work_report_dao.list_for_tenant(_require_tenant_id(user), report_type=report_type, limit=limit)
    return [
        WorkReportOut(
            id=str(r.id),
            author_type=r.author_type,
            author_id=str(r.author_id),
            report_type=r.report_type,
            period_date=r.period_date.isoformat(),
            content=r.content,
            source=r.source,
            created_at=r.created_at.isoformat() if r.created_at else "",
        )
        for r in reports
    ]


# ─── P4 Onboarding Endpoints ──────────────────────────────────────────────────


@router.get("/members-without-okr")
async def members_without_okr(user: UserRecord = Depends(get_current_user)) -> dict[str, Any]:
    """Return tracked members (those in OKR Agent's relationship list) who lack
    OKRs in the current period.  Also returns:
    - okr_agent_id        : UUID of the OKR Agent for the chat-link button
    - company_okr_exists  : bool - whether a company-level objective exists
    - tracked_user_ids    : UUIDs of all tracked platform users (for UI filtering)
    - tracked_agent_ids   : UUIDs of all tracked agents (for UI filtering)
    """
    settings = await _get_or_create_settings(_require_tenant_id(user))
    if not settings.enabled:
        raise HTTPException(403, "OKR is not enabled for this tenant")

    ps, pe = _compute_current_period(settings.period_frequency, settings.period_length_days)
    company_okr_exists = await okr_objective_dao.company_exists_for_period(
        _require_tenant_id(user), period_start=ps, period_end=pe
    )
    covered_ids = await okr_objective_dao.list_owner_ids_for_period(
        _require_tenant_id(user), period_start=ps, period_end=pe
    )

    okr_agent_id_val: uuid.UUID | None = settings.okr_agent_id
    okr_agent_id_str: str | None = str(okr_agent_id_val) if okr_agent_id_val else None

    tracked_user_ids: list[str] = []
    tracked_agent_ids: list[str] = []
    members_without_okr: list[MemberWithoutOkr] = []

    if okr_agent_id_val:
        rel_rows = await agent_relationship_dao.list_for_agent_with_members_and_providers(okr_agent_id_val)
        all_member_rows: list[TrackedMember] = []
        for rel in rel_rows:
            m = rel.member
            if not m or m.status != "active":
                continue
            all_member_rows.append((m.id, m.name, m.user_id, m.external_id, m.avatar_url, rel.provider_name))

        best_by_ext: dict[str, TrackedMember] = {}
        unkeyed: list[TrackedMember] = []
        for member in all_member_rows:
            member_id, member_name, member_user_id, external_id, avatar_url, provider_name = member
            if not external_id:
                unkeyed.append(member)
                continue
            existing = best_by_ext.get(external_id)
            if existing is None or (existing[2] is None and member_user_id is not None):
                best_by_ext[external_id] = member

        candidates = list(best_by_ext.values()) + unkeyed
        seen_user_ids: set[uuid.UUID] = set()
        canonical_members: list[TrackedMember] = []
        for member in candidates:
            member_id, member_name, member_user_id, external_id, avatar_url, provider_name = member
            if member_user_id is not None:
                if member_user_id in seen_user_ids:
                    continue
                seen_user_ids.add(member_user_id)
            canonical_members.append(member)

        for member_id, member_name, member_user_id, _external_id, avatar_url, provider_name in canonical_members:
            if member_user_id is not None:
                tracked_user_ids.append(str(member_user_id))
                if member_user_id not in covered_ids and member_id not in covered_ids:
                    members_without_okr.append(
                        {
                            "id": str(member_id),
                            "type": "user",
                            "display_name": member_name or "",
                            "avatar_url": avatar_url or "",
                            "channel": provider_name or None,
                            "channel_user_id": None,
                            "source_label": provider_name or "Platform User",
                        }
                    )
            else:
                if member_id not in covered_ids:
                    members_without_okr.append(
                        {
                            "id": str(member_id),
                            "type": "user",
                            "display_name": member_name or "",
                            "avatar_url": avatar_url or "",
                            "channel": provider_name or None,
                            "channel_user_id": None,
                            "source_label": provider_name or "Platform User",
                        }
                    )

        for agent in await agent_agent_relationship_dao.list_target_agents(
            okr_agent_id_val, exclude_system=True, exclude_statuses=["stopped", "error"]
        ):
            tracked_agent_ids.append(str(agent.id))
            if agent.id not in covered_ids:
                members_without_okr.append(
                    {
                        "id": str(agent.id),
                        "type": "agent",
                        "display_name": agent.name or "",
                        "avatar_url": agent.avatar_url or "",
                        "channel": None,
                        "channel_user_id": None,
                        "source_label": None,
                    }
                )

    if not okr_agent_id_val or (not tracked_user_ids and not tracked_agent_ids):
        for row in await agent_dao.list_id_name_avatar_active_nonsystem(_require_tenant_id(user)):
            mapping = object_mapping_from(row)
            row_id = uuid_from_row(mapping["id"])
            agent_id = str(row_id)
            tracked_agent_ids.append(agent_id)
            if row_id not in covered_ids:
                members_without_okr.append(
                    {
                        "id": agent_id,
                        "type": "agent",
                        "display_name": str_from_row(mapping.get("name")),
                        "avatar_url": str_from_row(mapping.get("avatar_url")) or "",
                        "channel": None,
                        "channel_user_id": None,
                    }
                )

        for row in await user_dao.list_id_name_avatar_for_tenant(_require_tenant_id(user)):
            mapping = object_mapping_from(row)
            row_id = uuid_from_row(mapping["id"])
            user_id = str(row_id)
            tracked_user_ids.append(user_id)
            if row_id not in covered_ids:
                members_without_okr.append(
                    {
                        "id": user_id,
                        "type": "user",
                        "display_name": str_from_row(mapping.get("display_name")),
                        "avatar_url": str_from_row(mapping.get("avatar_url")) or "",
                        "channel": None,
                        "channel_user_id": None,
                    }
                )

    last_outreach_error: dict[str, Any] | None = None
    if okr_agent_id_val:
        notif = await notification_dao.latest_system_task_failed(user_id=user.id, ref_id=okr_agent_id_val)
        if notif:
            last_outreach_error = {
                "message": notif.body,
                "timestamp": notif.created_at.isoformat() if notif.created_at else "",
                "is_read": notif.is_read,
            }

    channel_warnings: list[ChannelWarning] = []
    if okr_agent_id_val and members_without_okr:
        member_channels: dict[str, list[str]] = {}
        for m in members_without_okr:
            ch = m.get("channel") or m.get("source_label")
            if ch and ch not in ("Platform User", "Web"):
                member_channels.setdefault(ch, []).append(m.get("display_name", "?"))

        if member_channels:
            _channel_name_to_type = {
                "feishu": "feishu",
                "Feishu": "feishu",
                "dingtalk": "dingtalk",
                "DingTalk": "dingtalk",
                "wecom": "wecom",
                "WeCom": "wecom",
                "slack": "slack",
                "Slack": "slack",
                "discord": "discord",
                "Discord": "discord",
                "wechat": "wechat",
                "WeChat": "wechat",
            }
            needed_types: set[str] = set()
            for ch_name in member_channels:
                ct = _channel_name_to_type.get(ch_name)
                if ct:
                    needed_types.add(ct)

            if needed_types:
                configured_types = await channel_config_dao.list_configured_types_for_agent(
                    okr_agent_id_val, channel_types=list(needed_types)
                )
                missing_types = needed_types - configured_types
                _type_to_display: dict[str, str] = {
                    value: name for name, value in _channel_name_to_type.items() if name[0].isupper()
                }
                for mt in missing_types:
                    display_name = _type_to_display.get(mt) or mt
                    affected = []
                    for ch_name, names in member_channels.items():
                        if _channel_name_to_type.get(ch_name) == mt:
                            affected.extend(names)
                    channel_warnings.append(
                        {
                            "channel_type": mt,
                            "channel_display": display_name,
                            "affected_members": affected,
                            "count": len(affected),
                        }
                    )

    return {
        "period_start": ps.isoformat(),
        "period_end": pe.isoformat(),
        "company_okr_exists": company_okr_exists,
        "okr_agent_id": okr_agent_id_str,
        "members_without_okr": members_without_okr,
        "tracked_user_ids": tracked_user_ids,
        "tracked_agent_ids": tracked_agent_ids,
        "total": len(members_without_okr),
        "last_outreach_error": last_outreach_error,
        "channel_warnings": channel_warnings,
    }


@router.post("/trigger-member-outreach")
async def trigger_member_outreach(user: UserRecord = Depends(get_current_user)) -> dict[str, Any]:
    """Admin-initiated trigger: instruct the OKR Agent to contact all tracked
    members who haven't set their OKRs for the current period.

    Data flow:
      1. Backend queries tracked members (from AgentRelationship) who lack OKRs.
      2. Backend injects up to 3 recent chat messages per member as context.
      3. Builds a structured prompt and fires run_agent_oneshot as a background task.
      4. The OKR Agent LLM loop sends personalised messages via the correct channel,
         then reports success/failure back to the triggering admin.

    Returns immediately with status=accepted.
    """
    settings = await _get_or_create_settings(_require_tenant_id(user))
    if not settings.enabled:
        raise HTTPException(403, "OKR is not enabled for this tenant")

    ps, pe = _compute_current_period(settings.period_frequency, settings.period_length_days)

    if not settings.okr_agent_id:
        raise HTTPException(
            404,
            "OKR Agent not found. Please ensure OKR is enabled and the agent has been seeded.",
        )
    okr_agent = await agent_dao.get(settings.okr_agent_id)
    if not okr_agent:
        raise HTTPException(
            404,
            "OKR Agent not found. Please ensure OKR is enabled and the agent has been seeded.",
        )

    covered_ids = await okr_objective_dao.list_owner_ids_for_period(
        _require_tenant_id(user), period_start=ps, period_end=pe
    )
    company_okrs = await okr_objective_dao.list_for_period(
        _require_tenant_id(user), period_start=ps, period_end=pe, owner_types=["company"]
    )
    company_okr_krs: dict[uuid.UUID, Sequence[OKRKeyResultRecord]] = {}
    for co in company_okrs:
        company_okr_krs[co.id] = await okr_key_result_dao.list_for_objective(co.id)

    rel_rows = await agent_relationship_dao.list_for_agent_with_members(okr_agent.id, active_only=True)
    tracked_agents = await agent_agent_relationship_dao.list_target_agents(
        okr_agent.id, exclude_system=True, exclude_statuses=["stopped", "error"]
    )

    member_user_ids: dict[uuid.UUID, uuid.UUID | None] = {}
    for rel in rel_rows:
        org_member = rel.member
        if not org_member:
            continue
        member_user_ids[org_member.id] = org_member.user_id
        if not org_member.user_id:
            patterns = []
            if org_member.open_id:
                patterns.append(f"feishu_p2p_{org_member.open_id}")
            if org_member.external_id:
                patterns.append(f"feishu_p2p_{org_member.external_id}")
                patterns.append(f"dingtalk_p2p_{org_member.external_id}")
            if patterns:
                found = await chat_session_dao.find_user_id_by_external_patterns(
                    agent_id=okr_agent.id, patterns=patterns
                )
                if found:
                    member_user_ids[org_member.id] = found

    async def _recent_msgs(target_user_id: uuid.UUID | None) -> list[tuple[str, str, datetime]]:
        if not target_user_id:
            return []
        msgs = await chat_message_dao.list_recent_for_agent_user(agent_id=okr_agent.id, user_id=target_user_id, limit=3)
        return [(m.role, m.content, created) for m in msgs if (created := m.created_at) is not None]

    admin_username = await user_dao.display_name_for_id(user.id) or str(user.id)

    members_to_contact: list[str] = []
    index = 1

    for rel in rel_rows:
        org_member = rel.member
        if not org_member:
            continue
        platform_uid = member_user_ids.get(org_member.id)
        if platform_uid and platform_uid in covered_ids:
            continue

        msgs = await _recent_msgs(platform_uid) if platform_uid else []

        has_channel = bool(org_member.open_id or org_member.external_id)
        if has_channel:
            channel_hint = f'send_channel_message(member_name="{org_member.name}", message=...)'
            if platform_uid:
                channel_hint += "  (They also have a Platform account, but prefer channel message here)"
        elif platform_uid:
            channel_hint = 'send_platform_message(username="<their_username>", message=...)'
        else:
            channel_hint = "No channel available - note this in your summary"

        if msgs:
            history_lines = []
            for role, content, created_at in msgs:
                ts = created_at.strftime("%m-%d %H:%M") if created_at else ""
                speaker = "You" if role == "assistant" else org_member.name
                history_lines.append(f"  [{ts}] {speaker}: {content[:120]}")
            history_str = "\n".join(history_lines)
        else:
            history_str = "  (No previous conversation - treat this as first contact)"

        username_hint = ""
        if platform_uid:
            display = await user_dao.display_name_for_id(platform_uid)
            if display:
                username_hint = (
                    f'\n  Platform account: "{display}"'
                    + "  (use this as the recipient identifier in send_platform_message)"
                )

        member_block = (
            f"--- Member {index}: {org_member.name} ---\n"
            + f"  Type: Channel member{username_hint}\n"
            + f"  How to send: {channel_hint}\n"
            + "  Recent chat history (last 3 messages):\n"
            + f"{history_str}"
        )
        members_to_contact.append(member_block)
        index += 1

    for agent_member in tracked_agents:
        if agent_member.id in covered_ids:
            continue
        member_block = (
            f"--- Member {index}: {agent_member.name} [Agent] ---\n"
            + f'  STEP 1 → send_message_to_agent(agent_name="{agent_member.name}",\n'
            + '             message="[OKR Agent] Based on the company OKRs, describe your primary Objectives and Key Results '
            + f'for this period ({ps.isoformat()} ~ {pe.isoformat()}).")\n'
            + "  STEP 2 → Read the reply carefully from the tool result.\n"
            + "  STEP 3 → Call this EXACTLY (use the UUID below verbatim, do NOT invent one):\n"
            + '    create_objective(title="<their objective>", owner_type="agent",\n'
            + f'                    owner_id="{agent_member.id}",\n'
            + f'                    period_start="{ps.isoformat()}", period_end="{pe.isoformat()}")\n'
            + "  STEP 4 → For EACH Key Result they mentioned:\n"
            + '    create_key_result(objective_id="<id from STEP 3 result>",\n'
            + '                     title="<KR title>", target_value=<number>, unit="<unit if stated>")'
        )
        members_to_contact.append(member_block)
        index += 1

    if not members_to_contact:
        return {
            "status": "no_action",
            "message": "All tracked members already have OKRs set for this period. No outreach needed.",
            "okr_agent_id": str(okr_agent.id),
        }

    # ── Compose the final task prompt ─────────────────────────────────────────
    period_label = f"{ps.strftime('%Y-%m-%d')} to {pe.strftime('%Y-%m-%d')}"
    members_block = "\n\n".join(members_to_contact)

    # Build company OKR + KR context summary
    if company_okrs:
        company_okr_lines = []
        for i, co in enumerate(company_okrs, 1):
            company_okr_lines.append(f"  {i}. **{co.title}**")
            if co.description:
                company_okr_lines.append(f"     Description: {co.description[:120]}")
            krs = company_okr_krs.get(co.id, [])
            for j, kr in enumerate(krs, 1):
                target_str = f"(Target: {kr.target_value} {kr.unit or ''})" if kr.target_value else ""
                company_okr_lines.append(f"     KR{j}: {kr.title}{target_str}")
        company_okrs_block = "\n".join(company_okr_lines)
    else:
        company_okrs_block = "  (No company OKRs set yet for this period)"

    # Count agent vs human members for adaptive max_rounds
    n_agents = sum(1 for m in members_to_contact if "[Agent]" in m)
    n_humans = len(members_to_contact) - n_agents
    # human: 2 rounds (compose + send); agent: 6 rounds (send + reply + objective + 3 KRs)
    safe_max_rounds = n_humans * 2 + n_agents * 6 + 3

    task_prompt = f"""[ADMIN TRIGGER - OKR Member Outreach - ONE-SHOT TASK]

Current OKR period: {period_label}
Admin who triggered this: {admin_username}

━━━ COMPANY OBJECTIVES (share this context with each member) ━━━
{company_okrs_block}

━━━ YOUR TASK ━━━
Contact the {len(members_to_contact)} member(s) below who have NOT set their OKRs for this period.
• For [Agent] members: collect their OKR and record it immediately (see STEP 1-4 per member).
• For human members: send a warm reminder that includes the company OKR context above.

━━━ TOOL RULES (MANDATORY - DO NOT DEVIATE) ━━━
• For members tagged [Agent]:
  → Follow the STEP 1-4 sequence in their block exactly.
  → Use ONLY send_message_to_agent - never channel tools for agents.
• For human members:
  → If Platform account shown: send_platform_message(username="<display_name>", message="...")
  → If Feishu/DingTalk channel: send_channel_message(member_name="<name>", message="...")
  → If neither: skip and note in summary.
  → Humans are fire-and-forget - do NOT wait for their reply.

━━━ STEP-BY-STEP ━━━
1. Process each member in order, following per-member instructions.
2. If a send or create fails: log the failure and continue.
3. STOP completely after processing all members - do not respond further.

━━━ MEMBERS TO CONTACT ({len(members_to_contact)} total) ━━━

{members_block}

━━━ BEGIN NOW ━━━
"""

    # ── Launch background task ────────────────────────────────────────────────
    from app.api.background_tasks import schedule_background_task
    from app.services.heartbeat import run_agent_oneshot

    _ = schedule_background_task(
        run_agent_oneshot(
            agent_id=okr_agent.id,
            prompt=task_prompt,
            triggered_by_user_id=user.id,
            max_rounds=safe_max_rounds,
        ),
        "run OKR report",
    )

    return {
        "status": "accepted",
        "message": (
            f"OKR Agent outreach task triggered for {len(members_to_contact)} member(s). "
            + "You can check the conversation details in the OKR Agent's chat history."
        ),
        "okr_agent_id": str(okr_agent.id),
        "members_count": len(members_to_contact),
    }


@router.post("/trigger-daily-collection")
async def trigger_daily_collection(user: UserRecord = Depends(get_current_user)) -> dict[str, Any]:
    """Admin-triggered daily collection for tracked OKR relationships only."""
    if getattr(user, "role", None) not in ("org_admin", "platform_admin"):
        raise HTTPException(403, "Only org admins can trigger daily collection")
    from app.services.okr_daily_collection import trigger_daily_collection_for_tenant

    try:
        result = await trigger_daily_collection_for_tenant(_require_tenant_id(user))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    if result["total_targets"] == 0:
        return {
            "status": "no_action",
            "message": "OKR Agent has no tracked relationships to collect from.",
            "okr_agent_id": result["okr_agent_id"],
            "member_count": 0,
        }

    return {
        "status": "accepted",
        "message": (
            f"Daily OKR collection sent to {result['sent_humans']} human target(s) and "
            + f"{result['sent_agents']} agent target(s). Reply triggers are now active."
        ),
        "okr_agent_id": result["okr_agent_id"],
        "member_count": result["total_targets"],
    }
