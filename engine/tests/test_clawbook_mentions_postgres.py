from collections.abc import Generator, Iterable, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from tests.test_clawbook_likes_postgres import clawbook_db

from app.api import clawbook
from app.core.notification_kinds import NotificationKind
from app.dao.agent_dao import agent_dao
from app.dao.clawbook_dao import clawbook_post_dao
from app.dao.mention_patterns import like_patterns
from app.dao.user_dao import user_dao
from app.db.connection import DbConnection, Params, Row
from app.services import notification_service
from app.services.agent_tool_exec import clawbook as agent_clawbook

__all__ = ["clawbook_db"]

_OLDEST = datetime(2025, 1, 1, tzinfo=UTC)
_OLDER = datetime(2026, 1, 1, tzinfo=UTC)
_NEWER = datetime(2026, 3, 1, tzinfo=UTC)
_CREATOR_ID = UUID(int=0xC0FFEE)

type Recipient = tuple[str, UUID]


async def _tenant(db: DbConnection) -> UUID:
    tenant_id = uuid4()
    await db.execute(
        "INSERT INTO tenants (id, name, slug, tier) VALUES (%(id)s, %(name)s, %(slug)s, 'pro')",
        {"id": tenant_id, "name": f"QA {tenant_id}", "slug": f"qa-{tenant_id.hex}"},
    )
    return tenant_id


async def _user(
    db: DbConnection,
    tenant_id: UUID | None,
    display_name: str,
    *,
    active: bool = True,
    created_at: datetime = _NEWER,
    user_id: UUID | None = None,
) -> UUID:
    user_id = user_id or uuid4()
    await db.execute(
        "INSERT INTO users (id, tenant_id, display_name, is_active, created_at) "
        "VALUES (%(id)s, %(tenant)s, %(name)s, %(active)s, %(created_at)s)",
        {"id": user_id, "tenant": tenant_id, "name": display_name, "active": active, "created_at": created_at},
    )
    return user_id


async def _agent(
    db: DbConnection,
    tenant_id: UUID | None,
    name: str,
    *,
    status: str = "running",
    created_at: datetime = _NEWER,
    agent_id: UUID | None = None,
) -> UUID:
    agent_id = agent_id or uuid4()
    await db.execute(
        "INSERT INTO users (id, display_name, is_active) VALUES (%(id)s, 'Seed creator', false) ON CONFLICT DO NOTHING",
        {"id": _CREATOR_ID},
    )
    await db.execute(
        "INSERT INTO agents (id, name, role_description, creator_id, tenant_id, status, created_at, max_triggers, "
        "min_poll_interval_min, webhook_rate_limit, access_mode, company_access_level, heartbeat_interval_minutes) "
        "VALUES (%(id)s, %(name)s, 'QA', %(creator)s, %(tenant)s, %(status)s, %(created_at)s, 10, 5, 10, "
        "'company', 'use', 30)",
        {
            "id": agent_id,
            "name": name,
            "creator": _CREATOR_ID,
            "tenant": tenant_id,
            "status": status,
            "created_at": created_at,
        },
    )
    return agent_id


async def _fillers(db: DbConnection, tenant_id: UUID, count: int) -> None:
    await db.execute(
        "INSERT INTO users (id, tenant_id, display_name) SELECT gen_random_uuid(), %(tenant)s, 'Member ' || n "
        "FROM generate_series(1, %(count)s) AS n",
        {"tenant": tenant_id, "count": count},
    )
    await db.execute(
        "INSERT INTO users (id, display_name, is_active) VALUES (%(id)s, 'Seed creator', false) ON CONFLICT DO NOTHING",
        {"id": _CREATOR_ID},
    )
    await db.execute(
        "INSERT INTO agents (id, name, role_description, creator_id, tenant_id, max_triggers, min_poll_interval_min, "
        "webhook_rate_limit, access_mode, company_access_level, heartbeat_interval_minutes) "
        "SELECT gen_random_uuid(), 'Agent ' || n, 'QA', %(creator)s, %(tenant)s, 10, 5, 10, 'company', 'use', 30 "
        "FROM generate_series(1, %(count)s) AS n",
        {"creator": _CREATOR_ID, "tenant": tenant_id, "count": count},
    )


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    captured: list[dict[str, object]] = []

    async def capture(**kwargs: object) -> None:
        captured.append(kwargs)

    monkeypatch.setattr(notification_service, "send_notification", capture)
    return captured


def _agent_recipient(agent_id: UUID) -> Recipient:
    return ("agent_id", agent_id)


def _user_recipient(user_id: UUID) -> Recipient:
    return ("user_id", user_id)


def _mention(
    recipient: Recipient, author: str, content: str, post_id: UUID, *, where: str = "a post"
) -> dict[str, object]:
    key, recipient_id = recipient
    return {
        key: recipient_id,
        "notification_kind": NotificationKind.MENTION,
        "title": f"{author} mentioned you in {where}",
        "body": content[:150],
        "link": f"/clawbook?post={post_id}",
        "ref_id": post_id,
        "sender_name": author,
    }


def _mentions(
    recipients: Iterable[Recipient], author: str, content: str, post_id: UUID, *, where: str = "a post"
) -> list[dict[str, object]]:
    return [_mention(recipient, author, content, post_id, where=where) for recipient in recipients]


class StatementLog:
    """Every statement sent through ``DbConnection`` while recording, with the number of rows it returned."""

    def __init__(self) -> None:
        self.entries: list[tuple[str, int]] = []
        self.active = False

    def add(self, query: str, rows: int) -> None:
        if self.active:
            self.entries.append((query, rows))

    @contextmanager
    def recording(self) -> Generator[StatementLog]:
        self.entries.clear()
        self.active = True
        try:
            yield self
        finally:
            self.active = False


@pytest.fixture
def statement_log(monkeypatch: pytest.MonkeyPatch) -> StatementLog:
    log = StatementLog()
    execute, executemany = DbConnection.execute, DbConnection.executemany
    fetchone, fetchall = DbConnection.fetchone, DbConnection.fetchall

    async def logged_execute(self: DbConnection, query: str, params: Params = None) -> None:
        log.add(query, 0)
        await execute(self, query, params)

    async def logged_executemany(self: DbConnection, query: str, params_seq: Sequence[Params]) -> None:
        log.add(query, 0)
        await executemany(self, query, params_seq)

    async def logged_fetchone(self: DbConnection, query: str, params: Params = None) -> Row | None:
        row = await fetchone(self, query, params)
        log.add(query, int(row is not None))
        return row

    async def logged_fetchall(self: DbConnection, query: str, params: Params = None) -> list[Row]:
        rows = await fetchall(self, query, params)
        log.add(query, len(rows))
        return rows

    monkeypatch.setattr(DbConnection, "execute", logged_execute)
    monkeypatch.setattr(DbConnection, "executemany", logged_executemany)
    monkeypatch.setattr(DbConnection, "fetchone", logged_fetchone)
    monkeypatch.setattr(DbConnection, "fetchall", logged_fetchall)
    return log


async def test_a_tenant_post_notifies_each_named_recipient_under_todays_rules(
    clawbook_db: DbConnection, sent: list[dict[str, object]]
) -> None:
    tenant, foreign = await _tenant(clawbook_db), await _tenant(clawbook_db)
    # The newer Bob goes in first, so insertion order cannot be what picks the winner.
    _ = await _agent(clawbook_db, tenant, "Bob", created_at=_NEWER)
    older_bob = await _agent(clawbook_db, tenant, "Bob", created_at=_OLDER)
    stopped = await _agent(clawbook_db, tenant, "Stopper", status="stopped")
    _ = await _agent(clawbook_db, tenant, "Alice")
    _ = await _agent(clawbook_db, foreign, "Bob", created_at=_OLDEST)
    _ = await _user(clawbook_db, tenant, "Gone", active=False)
    nameless = await _user(clawbook_db, tenant, "")
    author = await _user(clawbook_db, tenant, "Quinn")
    _ = await _user(clawbook_db, foreign, "Cara")
    post_id = uuid4()
    content = "@Bob @Stopper @Gone @USER @Alice, @Quinn @Cara " + "x" * 200

    await clawbook._notify_mentions(content, author, "Quinn", post_id, tenant)

    assert sent == _mentions(
        [_agent_recipient(older_bob), _agent_recipient(stopped), _user_recipient(nameless)], "Quinn", content, post_id
    )


async def test_trailing_punctuation_stays_part_of_the_token(
    clawbook_db: DbConnection, sent: list[dict[str, object]]
) -> None:
    tenant = await _tenant(clawbook_db)
    alice = await _agent(clawbook_db, tenant, "Alice")
    post_id = uuid4()

    await clawbook._notify_mentions("@Alice, @alice. @(Alice)", uuid4(), "Quinn", post_id, tenant)
    assert sent == []

    await clawbook._notify_mentions("@Alice", uuid4(), "Quinn", post_id, tenant)
    assert sent == _mentions([_agent_recipient(alice)], "Quinn", "@Alice", post_id)


async def test_an_agent_author_is_excluded_from_its_own_mention(
    clawbook_db: DbConnection, sent: list[dict[str, object]]
) -> None:
    tenant = await _tenant(clawbook_db)
    robo = await _agent(clawbook_db, tenant, "Robo")
    bob = await _agent(clawbook_db, tenant, "Bob")
    robo_user = await _user(clawbook_db, tenant, "Robo")
    post_id = uuid4()
    content = "@Robo @Bob"

    await clawbook._notify_mentions(content, robo, "Robo", post_id, tenant)

    assert sent == _mentions([_user_recipient(robo_user), _agent_recipient(bob)], "Robo", content, post_id)


@pytest.mark.parametrize("older_first", [True, False])
async def test_duplicate_names_notify_the_oldest_record_of_each_kind_once(
    clawbook_db: DbConnection, sent: list[dict[str, object]], older_first: bool
) -> None:
    tenant = await _tenant(clawbook_db)
    inserted: dict[datetime, tuple[UUID, UUID]] = {}
    for created_at in [_OLDER, _NEWER] if older_first else [_NEWER, _OLDER]:
        inserted[created_at] = (
            await _agent(clawbook_db, tenant, "Bob", created_at=created_at),
            await _user(clawbook_db, tenant, "Bob", created_at=created_at),
        )
    older_agent, older_user = inserted[_OLDER]
    post_id = uuid4()
    content = "@Bob @bob @BOB"

    await clawbook._notify_mentions(content, uuid4(), "Quinn", post_id, tenant)

    assert sent == _mentions([_agent_recipient(older_agent), _user_recipient(older_user)], "Quinn", content, post_id)


@pytest.mark.parametrize("lowest_first", [True, False])
async def test_equally_old_duplicates_resolve_to_the_lowest_id(
    clawbook_db: DbConnection, sent: list[dict[str, object]], lowest_first: bool
) -> None:
    tenant = await _tenant(clawbook_db)
    agent_ids, user_ids = [UUID(int=1), UUID(int=2)], [UUID(int=3), UUID(int=4)]
    for agent_id in agent_ids if lowest_first else reversed(agent_ids):
        _ = await _agent(clawbook_db, tenant, "Bob", agent_id=agent_id)
    for user_id in user_ids if lowest_first else reversed(user_ids):
        _ = await _user(clawbook_db, tenant, "Bob", user_id=user_id)
    post_id = uuid4()

    await clawbook._notify_mentions("@Bob", uuid4(), "Quinn", post_id, tenant)

    assert sent == _mentions([_agent_recipient(agent_ids[0]), _user_recipient(user_ids[0])], "Quinn", "@Bob", post_id)


async def test_vietnamese_names_match_in_any_case_and_lookalike_names_do_not(
    clawbook_db: DbConnection, sent: list[dict[str, object]]
) -> None:
    tenant = await _tenant(clawbook_db)
    duc = await _user(clawbook_db, tenant, "Đức")
    _ = await _user(clawbook_db, tenant, "Duc")
    nguyen = await _agent(clawbook_db, tenant, "NGUYỄN")
    _ = await _agent(clawbook_db, tenant, "Đưc")
    _ = await _agent(clawbook_db, tenant, "Nguyên")
    post_id = uuid4()
    content = "@đức @ĐỨC @nguyễn @NGUYỄN"

    await clawbook._notify_mentions(content, uuid4(), "Quinn", post_id, tenant)

    assert sent == _mentions([_user_recipient(duc), _agent_recipient(nguyen)], "Quinn", content, post_id)
    candidates = await clawbook_db.fetchall(
        "SELECT name FROM agents WHERE tenant_id = %(tenant)s AND name ILIKE ANY(%(patterns)s) ORDER BY name",
        {"tenant": tenant, "patterns": like_patterns(["đức"])},
    )
    assert {row["name"] for row in candidates} >= {"Đưc"}
    found = await agent_dao.find_by_mention_names({"đức", "nguyễn"}, tenant_id=tenant, exclude_id=None)
    assert {key: agent.name for key, agent in found.items()} == {"nguyễn": "NGUYỄN"}


async def test_a_post_without_a_tenant_keeps_the_database_wide_scope_for_active_users(
    clawbook_db: DbConnection, sent: list[dict[str, object]]
) -> None:
    first, second = await _tenant(clawbook_db), await _tenant(clawbook_db)
    older_bob = await _agent(clawbook_db, first, "Bob", created_at=_OLDER)
    _ = await _agent(clawbook_db, second, "Bob", created_at=_NEWER)
    tenantless_agent = await _agent(clawbook_db, None, "Tess")
    ana = await _user(clawbook_db, first, "Ana")
    zed = await _user(clawbook_db, None, "Zed")
    _ = await _user(clawbook_db, second, "Gone", active=False)
    post_id = uuid4()
    content = "@Bob @Ana @Zed @Tess @Gone"

    await clawbook._notify_mentions(content, uuid4(), "Quinn", post_id, None)

    assert sent == _mentions(
        [
            _agent_recipient(older_bob),
            _user_recipient(ana),
            _user_recipient(zed),
            _agent_recipient(tenantless_agent),
        ],
        "Quinn",
        content,
        post_id,
    )


@pytest.mark.parametrize("operation", ["post", "comment"])
async def test_agent_tool_mentions_notify_agents_of_the_authors_tenant_only(
    clawbook_db: DbConnection, sent: list[dict[str, object]], operation: str
) -> None:
    tenant, foreign = await _tenant(clawbook_db), await _tenant(clawbook_db)
    robo = await _agent(clawbook_db, tenant, "Robo")
    bob = await _agent(clawbook_db, tenant, "Bob", created_at=_NEWER)
    _ = await _agent(clawbook_db, foreign, "Bob", created_at=_OLDEST)
    _ = await _user(clawbook_db, tenant, "Bob")
    content = "@Bob @Robo @bob"
    if operation == "post":
        result = await agent_clawbook._clawbook_create_post(robo, {"content": content})
        post_id = UUID(result.removeprefix("Post published! (ID: ").removesuffix(")"))
        where = "a clawbook post"
    else:
        post = await clawbook_post_dao.create_post(
            {
                "author_id": uuid4(),
                "author_type": "human",
                "author_name": "Mira",
                "content": "Hello",
                "tenant_id": tenant,
                "likes_count": 0,
                "comments_count": 0,
            }
        )
        post_id, where = post.id, "a comment"
        assert await agent_clawbook._clawbook_add_comment(robo, {"post_id": str(post_id), "content": content}) == (
            "Comment added to post by Mira."
        )

    mention_calls = [call for call in sent if call["notification_kind"] == NotificationKind.MENTION]
    assert mention_calls == _mentions([_agent_recipient(bob)], "Robo", content, post_id, where=where)


async def test_each_lookup_is_one_statement_however_large_the_roster(
    clawbook_db: DbConnection, sent: list[dict[str, object]], statement_log: StatementLog
) -> None:
    outcomes: dict[int, list[tuple[str, int]]] = {}
    for size in (5, 300):
        tenant = await _tenant(clawbook_db)
        await _fillers(clawbook_db, tenant, size)
        bob = await _agent(clawbook_db, tenant, "Bob")
        duc = await _user(clawbook_db, tenant, "Đức")
        sent.clear()
        post_id = uuid4()
        with statement_log.recording():
            await clawbook._notify_mentions("@Bob @đức", uuid4(), "Quinn", post_id, tenant)
        assert sent == _mentions([_agent_recipient(bob), _user_recipient(duc)], "Quinn", "@Bob @đức", post_id)
        outcomes[size] = list(statement_log.entries)
        assert [("FROM agents" in query, "FROM users" in query) for query, _ in outcomes[size]] == [
            (True, False),
            (False, True),
        ]

    assert [query for query, _ in outcomes[5]] == [query for query, _ in outcomes[300]]
    assert [rows for _, rows in outcomes[5]] == [rows for _, rows in outcomes[300]] == [1, 1]


async def test_the_database_wide_scope_is_also_one_statement_per_kind(
    clawbook_db: DbConnection, sent: list[dict[str, object]], statement_log: StatementLog
) -> None:
    for size in (5, 300):
        tenant = await _tenant(clawbook_db)
        await _fillers(clawbook_db, tenant, size)
    _ = await _agent(clawbook_db, await _tenant(clawbook_db), "Bob")
    _ = await _user(clawbook_db, None, "Zed")

    with statement_log.recording():
        await clawbook._notify_mentions("@Bob @Zed", uuid4(), "Quinn", uuid4(), None)

    assert [rows for _, rows in statement_log.entries] == [1, 1]
    assert len(sent) == 2


async def test_content_without_a_mention_and_empty_token_sets_issue_no_statement(
    clawbook_db: DbConnection, sent: list[dict[str, object]], statement_log: StatementLog
) -> None:
    tenant = await _tenant(clawbook_db)

    with statement_log.recording():
        await clawbook._notify_mentions("No one is tagged here", uuid4(), "Quinn", uuid4(), tenant)
        assert await agent_dao.find_by_mention_names(set(), tenant_id=tenant, exclude_id=None) == {}
        assert await user_dao.find_active_by_mention_names(set(), tenant_id=tenant, exclude_user_id=None) == {}

    assert statement_log.entries == []
    assert sent == []


_ILIKE_BY_COLLATION = {
    "default": "SELECT candidate FROM unnest(%(names)s::text[]) AS t(candidate) WHERE candidate ILIKE ANY(%(patterns)s)",
    "C": (
        "SELECT candidate FROM unnest(%(names)s::text[]) AS t(candidate) "
        'WHERE candidate COLLATE "C" ILIKE ANY(%(patterns)s)'
    ),
    "und-x-icu": (
        "SELECT candidate FROM unnest(%(names)s::text[]) AS t(candidate) "
        'WHERE candidate COLLATE "und-x-icu" ILIKE ANY(%(patterns)s)'
    ),
    "tr-x-icu": (
        "SELECT candidate FROM unnest(%(names)s::text[]) AS t(candidate) "
        'WHERE candidate COLLATE "tr-x-icu" ILIKE ANY(%(patterns)s)'
    ),
    "lt-x-icu": (
        "SELECT candidate FROM unnest(%(names)s::text[]) AS t(candidate) "
        'WHERE candidate COLLATE "lt-x-icu" ILIKE ANY(%(patterns)s)'
    ),
}
_COLLATION_NAMES = (
    "Bob",
    "BOB",
    "Đức",
    "ĐỨC",
    "Nguyễn",
    "NGUYỄN",
    "\u0130stanbul",
    "ISTANBUL",
    "Istanbul",
    "\u212aelvin",
    "Kim",
    "KIM",
    "ΛΔ\u03a3",
    "Straße",
    "田中",
    "50%_off",
    "a\\b",
    "i\u0307",
    "\u0130",
    "\u0131",
    "TR\u00cd",
    "\u00ccgnas",
    "J\u0301",
    "\u012e\u0301",
    "I\u0323\u0307",
)


async def _installed_or_skip(db: DbConnection, collation: str) -> None:
    if collation == "default":
        return
    if await db.fetchval("SELECT 1 FROM pg_collation WHERE collname = %(name)s", {"name": collation}) is None:
        pytest.skip(f"collation {collation} is not installed")


@pytest.mark.parametrize("collation", list(_ILIKE_BY_COLLATION))
async def test_the_candidates_are_a_superset_under_every_collation(clawbook_db: DbConnection, collation: str) -> None:
    await _installed_or_skip(clawbook_db, collation)

    missed = []
    for name in _COLLATION_NAMES:
        rows = await clawbook_db.fetchall(
            _ILIKE_BY_COLLATION[collation], {"names": [name], "patterns": like_patterns([name.lower()])}
        )
        if [row["candidate"] for row in rows] != [name]:
            missed.append(name)

    assert missed == []


@pytest.mark.parametrize(
    ("collation", "name"),
    [
        ("C", "\u212aelvin"),
        ("C", "\u0130"),
        ("C", "Đ"),
        ("tr-x-icu", "Istanbul"),
        ("tr-x-icu", "I\u0323\u0307"),
        ("lt-x-icu", "TR\u00cd"),
    ],
)
async def test_a_literal_pattern_really_misses_under_that_collation(
    clawbook_db: DbConnection, collation: str, name: str
) -> None:
    await _installed_or_skip(clawbook_db, collation)
    literal = [name.lower()]

    literal_rows = await clawbook_db.fetchall(_ILIKE_BY_COLLATION[collation], {"names": [name], "patterns": literal})
    ours = await clawbook_db.fetchall(
        _ILIKE_BY_COLLATION[collation], {"names": [name], "patterns": like_patterns(literal)}
    )

    assert literal_rows == []
    assert [row["candidate"] for row in ours] == [name]
