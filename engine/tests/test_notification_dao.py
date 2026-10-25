from typing import Literal
from uuid import UUID

import pytest

from app.core.notification_kinds import NotificationKind
from app.dao.notification_dao import notification_dao
from notification_db_fakes import NotificationConnection, notification_db

__all__ = ["notification_db"]

USER_ID = UUID(int=100)
pytestmark = pytest.mark.usefixtures("notification_db")
RETAINED_SCOPE = (
    "NOT EXISTS(SELECT 1 FROM company_transfer_retained_agents r "
    "WHERE notifications.agent_id=r.agent_id OR notifications.ref_id=r.agent_id "
    "OR notifications.ref_id IN (SELECT id FROM tasks WHERE agent_id=r.agent_id) "
    "OR notifications.ref_id IN (SELECT id FROM approval_requests WHERE agent_id=r.agent_id) "
    "OR notifications.link LIKE '%%' || r.agent_id::text || '%%')"
)


@pytest.mark.parametrize(
    "types",
    [
        None,
        [],
        [NotificationKind.BROADCAST],
        [NotificationKind.APPROVAL_PENDING, NotificationKind.COMPANY_ADMISSION_PENDING],
    ],
)
async def test_bulk_read_predicates_include_user_unread_and_optional_types(
    notification_db: NotificationConnection,
    types: list[NotificationKind] | None,
) -> None:
    # Given / When
    await notification_dao.mark_all_read_for_user(USER_ID, types=types)
    # Then
    query, params = notification_db.executed[0]
    expected = (
        "UPDATE notifications SET is_read = TRUE WHERE user_id = %(user_id)s "
        "AND is_read IS FALSE AND archived_at IS NULL"
    )
    expected += " AND " + RETAINED_SCOPE
    if types is not None:
        expected += " AND type = ANY(%(types)s)"
    assert query == expected
    assert params == (
        {"user_id": USER_ID} if types is None else {"user_id": USER_ID, "types": [kind.value for kind in types]}
    )
    if types is not None:
        bound_types = params["types"]
        assert isinstance(bound_types, list)
        assert all(type(value) is str for value in bound_types)


@pytest.mark.parametrize("types", [None, [], [NotificationKind.BROADCAST]])
@pytest.mark.parametrize("unread_only", [False, True])
@pytest.mark.parametrize("view", ["active", "archived"])
async def test_list_predicates_and_order_are_stable_and_parameterized(
    notification_db: NotificationConnection,
    types: list[NotificationKind] | None,
    unread_only: bool,
    view: Literal["active", "archived"],
) -> None:
    # Given / When
    result = await notification_dao.list_for_user(
        USER_ID, types=types, unread_only=unread_only, view=view, limit=7, offset=50
    )
    # Then
    query, params = notification_db.executed[0]
    where = "user_id = %(user_id)s AND " + RETAINED_SCOPE
    where += " AND archived_at IS NULL" if view == "active" else " AND archived_at IS NOT NULL"
    if unread_only:
        where += " AND is_read IS FALSE"
    if types is not None:
        where += " AND type = ANY(%(types)s)"
    assert f"WHERE {where} ORDER BY created_at DESC, id DESC OFFSET %(offset)s LIMIT %(limit)s" in query
    assert params == {
        "user_id": USER_ID,
        "limit": 7,
        "offset": 50,
        **({} if types is None else {"types": [kind.value for kind in types]}),
    }
    if types is not None:
        bound_types = params["types"]
        assert isinstance(bound_types, list)
        assert all(type(value) is str for value in bound_types)
    assert result == []


@pytest.mark.parametrize("types", [None, [], [NotificationKind.BROADCAST]])
async def test_count_predicates_match_bulk_scope(
    notification_db: NotificationConnection, types: list[NotificationKind] | None
) -> None:
    # Given / When
    result = await notification_dao.count_unread_for_user(USER_ID, types=types)
    # Then
    query, params = notification_db.executed[0]
    expected = (
        "SELECT COUNT(*) FROM notifications WHERE user_id = %(user_id)s AND is_read IS FALSE AND archived_at IS NULL"
    )
    expected += " AND " + RETAINED_SCOPE
    if types is not None:
        expected += " AND type = ANY(%(types)s)"
    assert query == expected
    assert params == (
        {"user_id": USER_ID} if types is None else {"user_id": USER_ID, "types": [kind.value for kind in types]}
    )
    if types is not None:
        bound_types = params["types"]
        assert isinstance(bound_types, list)
        assert all(type(value) is str for value in bound_types)
    assert result == 0
