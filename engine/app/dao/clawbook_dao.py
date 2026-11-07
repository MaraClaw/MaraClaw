"""DAO for clawbook posts, comments, and likes"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar, Literal, final, override
from uuid import UUID

from app.core.json_types import int_from_row, str_from_row, uuid_from_row
from app.dao.analytics_projection_contracts import ClawbookStatsProjection
from app.dao.base import BaseDAO
from app.records.clawbook import ClawbookCommentRecord, ClawbookLikeRecord, ClawbookPostRecord


@dataclass(frozen=True, slots=True)
class ClawbookPosition:
    created_at: datetime
    id: UUID


_POST_COLUMNS = (
    "id",
    "author_id",
    "author_type",
    "author_name",
    "content",
    "tenant_id",
    "likes_count",
    "comments_count",
    "created_at",
)

_COMMENT_COLUMNS = (
    "id",
    "post_id",
    "author_id",
    "author_type",
    "author_name",
    "content",
    "created_at",
)

_LIKE_COLUMNS = (
    "id",
    "post_id",
    "author_id",
    "author_type",
    "created_at",
)

# Hide posts/comments authored by system or non-company agents.
_HIDDEN_AGENT_EXISTS = (
    "EXISTS ("
    "SELECT 1 FROM agents a "
    "WHERE a.id = {author_col} "
    "AND (a.is_system IS TRUE OR COALESCE(a.access_mode, 'company') <> 'company')"
    ")"
)
_PRIVATE_OR_SYSTEM_POST = (
    f"(clawbook_posts.author_type = 'agent' AND {_HIDDEN_AGENT_EXISTS.format(author_col='clawbook_posts.author_id')})"
)


@final
class ClawbookPostDAO(BaseDAO[ClawbookPostRecord]):
    """DAO for clawbook_posts rows."""

    table: ClassVar[str] = "clawbook_posts"
    columns: ClassVar[tuple[str, ...]] = _POST_COLUMNS
    record_factory = staticmethod(ClawbookPostRecord.from_row)

    async def list_posts_recent(
        self,
        limit: int,
        tenant_id: UUID | None = None,
    ) -> Sequence[ClawbookPostRecord]:
        params: dict[str, Any] = {"limit": limit}
        tenant_sql = ""
        if tenant_id is not None:
            tenant_sql = " WHERE tenant_id = %(tenant_id)s"
            params["tenant_id"] = tenant_id
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM clawbook_posts{tenant_sql} ORDER BY created_at DESC LIMIT %(limit)s",
                params,
            )
            return [ClawbookPostRecord.from_row(row) for row in rows]

    async def list_feed(
        self,
        *,
        tenant_id: UUID | str | None = None,
        since: datetime | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> Sequence[ClawbookPostRecord]:
        """Public clawbook feed excluding system/private agent posts."""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        clauses = [f"NOT {_PRIVATE_OR_SYSTEM_POST}"]
        if tenant_id is not None:
            clauses.append("tenant_id = %(tenant_id)s")
            params["tenant_id"] = tenant_id
        if since is not None:
            clauses.append("created_at > %(since)s")
            params["since"] = since
        where_sql = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM clawbook_posts{where_sql} "
                "ORDER BY created_at DESC, id DESC LIMIT %(limit)s OFFSET %(offset)s",
                params,
            )
            return [ClawbookPostRecord.from_row(row) for row in rows]

    async def list_feed_page(
        self, *, tenant_id: UUID | None, before: ClawbookPosition | None = None, limit: int = 31
    ) -> Sequence[ClawbookPostRecord]:
        """Read a stable newest-first page without scanning preceding pages."""
        params: dict[str, UUID | datetime | int] = {"limit": limit}
        clauses = [f"NOT {_PRIVATE_OR_SYSTEM_POST}"]
        if tenant_id is not None:
            clauses.append("tenant_id = %(tenant_id)s")
            params["tenant_id"] = tenant_id
        if before is not None:
            clauses.append("(created_at, id) < (%(created_at)s, %(id)s)")
            params.update(created_at=before.created_at, id=before.id)
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM clawbook_posts WHERE {' AND '.join(clauses)} "
                "ORDER BY created_at DESC, id DESC LIMIT %(limit)s",
                params,
            )
            return [ClawbookPostRecord.from_row(row) for row in rows]

    async def get_stats(self, tenant_id: UUID | str | None = None) -> ClawbookStatsProjection:
        """Aggregate clawbook stats excluding system/private agent posts."""
        params: dict[str, object] = {}
        tenant_sql = ""
        if tenant_id is not None:
            tenant_sql = " AND clawbook_posts.tenant_id = %(tenant_id)s"
            params["tenant_id"] = tenant_id
        post_filter = f"NOT {_PRIVATE_OR_SYSTEM_POST}{tenant_sql}"
        async with self.session() as db:
            total_posts = await db.fetchval(
                f"SELECT COUNT(*) FROM clawbook_posts WHERE {post_filter}",
                params,
            )
            total_comments = await db.fetchval(
                "SELECT COUNT(*) FROM clawbook_comments "
                "JOIN clawbook_posts ON clawbook_comments.post_id = clawbook_posts.id "
                f"WHERE {post_filter}",
                params,
            )
            today_posts = await db.fetchval(
                "SELECT COUNT(*) FROM clawbook_posts WHERE created_at >= date_trunc('day', NOW() AT TIME ZONE 'UTC') "
                f"AND {post_filter}",
                params,
            )
            top_rows = await db.fetchall(
                "SELECT author_name, author_type, COUNT(*) AS post_count "
                f"FROM clawbook_posts WHERE {post_filter} "
                "GROUP BY author_name, author_type "
                "ORDER BY post_count DESC LIMIT 5",
                params,
            )
            return {
                "total_posts": int_from_row(total_posts),
                "total_comments": int_from_row(total_comments),
                "today_posts": int_from_row(today_posts),
                "top_contributors": [
                    {"name": row["author_name"], "type": row["author_type"], "posts": int_from_row(row["post_count"])}
                    for row in top_rows
                ],
            }

    async def get_post(self, post_id: UUID) -> ClawbookPostRecord | None:
        return await self.get(post_id)

    async def create_post(self, obj_in: Mapping[str, Any]) -> ClawbookPostRecord:
        return await self.create(obj_in=obj_in)

    async def increment_comments_count(self, post_id: UUID) -> ClawbookPostRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                "UPDATE clawbook_posts SET comments_count = COALESCE(comments_count, 0) + 1 "
                f"WHERE id = %(post_id)s RETURNING {self._select_list()}",
                {"post_id": post_id},
            )
            return ClawbookPostRecord.from_row(row) if row is not None else None

    @override
    async def delete(self, *, record_id: UUID) -> ClawbookPostRecord | None:
        """Delete a post with its likes and comments (their foreign keys do not cascade).

        The post row is locked first so likes, toggles and comments queue behind the delete instead of landing
        between the child and parent deletes. Everything joins the caller's transaction, so a rollback restores all rows.
        """
        params = {"post_id": record_id}
        async with self.session() as db:
            if await db.fetchone("SELECT id FROM clawbook_posts WHERE id = %(post_id)s FOR UPDATE", params) is None:
                return None
            _ = await db.execute("DELETE FROM clawbook_likes WHERE post_id = %(post_id)s", params)
            _ = await db.execute("DELETE FROM clawbook_comments WHERE post_id = %(post_id)s", params)
            return await super().delete(record_id=record_id)


@final
class ClawbookCommentDAO(BaseDAO[ClawbookCommentRecord]):
    """DAO for clawbook_comments rows."""

    table: ClassVar[str] = "clawbook_comments"
    columns: ClassVar[tuple[str, ...]] = _COMMENT_COLUMNS
    record_factory = staticmethod(ClawbookCommentRecord.from_row)

    async def list_comments_for_post(
        self,
        post_id: UUID,
        limit: int | None = 5,
        *,
        visible_only: bool = False,
        after: ClawbookPosition | None = None,
    ) -> Sequence[ClawbookCommentRecord]:
        params: dict[str, Any] = {"post_id": post_id}
        limit_sql = ""
        if limit is not None:
            limit_sql = " LIMIT %(limit)s"
            params["limit"] = limit
        clauses = ["post_id = %(post_id)s"]
        if visible_only:
            hidden = _HIDDEN_AGENT_EXISTS.format(author_col="clawbook_comments.author_id")
            clauses.append(f"NOT (author_type = 'agent' AND {hidden})")
        if after is not None:
            clauses.append("(created_at, id) > (%(created_at)s, %(id)s)")
            params.update(created_at=after.created_at, id=after.id)
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM clawbook_comments "
                f"WHERE {' AND '.join(clauses)} ORDER BY created_at ASC, id ASC{limit_sql}",
                params,
            )
            return [ClawbookCommentRecord.from_row(row) for row in rows]

    async def create_comment(self, obj_in: Mapping[str, Any]) -> ClawbookCommentRecord:
        return await self.create(obj_in=obj_in)

    async def list_distinct_comment_authors(
        self,
        post_id: UUID,
    ) -> list[tuple[UUID, str]]:
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT DISTINCT author_id, author_type FROM clawbook_comments WHERE post_id = %(post_id)s",
                {"post_id": post_id},
            )
            return [(uuid_from_row(row["author_id"]), str_from_row(row["author_type"])) for row in rows]


@final
class ClawbookLikeDAO(BaseDAO[ClawbookLikeRecord]):
    """DAO for clawbook_likes rows."""

    table: ClassVar[str] = "clawbook_likes"
    columns: ClassVar[tuple[str, ...]] = _LIKE_COLUMNS
    record_factory = staticmethod(ClawbookLikeRecord.from_row)

    async def toggle(self, post_id: UUID, author_id: UUID, author_type: Literal["human", "agent"]) -> bool | None:
        """Serialize on the post and repair its count in the same bound transaction."""
        async with self.session() as db:
            post = await db.fetchone(
                "SELECT id FROM clawbook_posts WHERE id = %(post_id)s FOR UPDATE", {"post_id": post_id}
            )
            if post is None:
                return None
            existing = await self.get_by_post_and_author(post_id, author_id)
            if existing is not None:
                _ = await self.delete_by_post_and_author(post_id, author_id)
            else:
                _ = await self.create(obj_in={"post_id": post_id, "author_id": author_id, "author_type": author_type})
            _ = await db.execute(
                "UPDATE clawbook_posts SET likes_count = (SELECT COUNT(*) FROM clawbook_likes WHERE post_id = %(post_id)s) "
                "WHERE id = %(post_id)s",
                {"post_id": post_id},
            )
            return existing is None

    async def get_by_post_and_author(self, post_id: UUID, author_id: UUID) -> ClawbookLikeRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM clawbook_likes "
                "WHERE post_id = %(post_id)s AND author_id = %(author_id)s LIMIT 1",
                {"post_id": post_id, "author_id": author_id},
            )
            return ClawbookLikeRecord.from_row(row) if row is not None else None

    async def delete_by_post_and_author(self, post_id: UUID, author_id: UUID) -> bool:
        async with self.session() as db:
            row = await db.fetchone(
                "DELETE FROM clawbook_likes WHERE post_id = %(post_id)s AND author_id = %(author_id)s RETURNING id",
                {"post_id": post_id, "author_id": author_id},
            )
            return row is not None


clawbook_post_dao = ClawbookPostDAO()
clawbook_comment_dao = ClawbookCommentDAO()
clawbook_like_dao = ClawbookLikeDAO()
