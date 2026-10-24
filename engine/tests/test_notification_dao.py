from typing import Literal
from uuid import UUID

import pytest

from app.dao.notification_dao import notification_dao
from notification_db_fakes import NotificationConnection, notification_db as notification_db

USER_ID = UUID(int=100)
pytestmark = pytest.mark.usefixtures("notification_db")


@pytest.mark.parametrize("types", [None, [], ["broadcast"], ["approval_pending", "company_admission_pending"]])
async def test_bulk_read_predicates_include_user_unread_and_optional_types(
    notification_db: NotificationConnection,
    types: list[str] | None,
) -> None:
    # Given / When
    await notification_dao.mark_all_read_for_user(USER_ID, types=types)
    # Then
    query, params = notification_db.executed[0]
    expected = (
        "UPDATE notifications SET is_read = TRUE WHERE user_id = %(user_id)s "
        "AND is_read IS FALSE AND archived_at IS NULL"
    )
    if types is not None:
        expected += " AND type = ANY(%(types)s)"
    assert query == expected
    assert params == ({"user_id": USER_ID} if types is None else {"user_id": USER_ID, "types": types})


@pytest.mark.parametrize("types", [None, [], ["broadcast"]])
@pytest.mark.parametrize("unread_only", [False, True])
@pytest.mark.parametrize("view", ["active", "archived"])
async def test_list_predicates_and_order_are_stable_and_parameterized(
    notification_db: NotificationConnection,
    types: list[str] | None,
    unread_only: bool,
    view: Literal["active", "archived"],
) -> None:
    # Given / When
    result = await notification_dao.list_for_user(
        USER_ID, types=types, unread_only=unread_only, view=view, limit=7, offset=50
    )
    # Then
    query, params = notification_db.executed[0]
    where = "user_id = %(user_id)s"
    where += " AND archived_at IS NULL" if view == "active" else " AND archived_at IS NOT NULL"
    if unread_only:
        where += " AND is_read IS FALSE"
    if types is not None:
        where += " AND type = ANY(%(types)s)"
    assert f"WHERE {where} ORDER BY created_at DESC, id DESC OFFSET %(offset)s LIMIT %(limit)s" in query
    assert params == {"user_id": USER_ID, "limit": 7, "offset": 50, **({} if types is None else {"types": types})}
    assert result == []


@pytest.mark.parametrize("types", [None, [], ["broadcast"]])
async def test_count_predicates_match_bulk_scope(
    notification_db: NotificationConnection, types: list[str] | None
) -> None:
    # Given / When
    result = await notification_dao.count_unread_for_user(USER_ID, types=types)
    # Then
    query, params = notification_db.executed[0]
    expected = (
        "SELECT COUNT(*) FROM notifications WHERE user_id = %(user_id)s AND is_read IS FALSE AND archived_at IS NULL"
    )
    if types is not None:
        expected += " AND type = ANY(%(types)s)"
    assert query == expected
    assert params == ({"user_id": USER_ID} if types is None else {"user_id": USER_ID, "types": types})
    assert result == 0
