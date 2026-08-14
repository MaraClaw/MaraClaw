"""DAO for plaza posts, comments, and likes (psycopg)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, ClassVar
from uuid import UUID

from app.dao.base import BaseDAO
from app.records.plaza import PlazaCommentRecord, PlazaLikeRecord, PlazaPostRecord

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
    + "SELECT 1 FROM agents a "
    + "WHERE a.id = {author_col} "
    + "AND (a.is_system IS TRUE OR COALESCE(a.access_mode, 'company') <> 'company')"
    + ")"
)
_PRIVATE_OR_SYSTEM_POST = (
    f"(plaza_posts.author_type = 'agent' AND {_HIDDEN_AGENT_EXISTS.format(author_col='plaza_posts.author_id')})"
)


class PlazaPostDAO(BaseDAO[PlazaPostRecord]):
    """DAO for plaza_posts rows."""

    table: ClassVar[str] = "plaza_posts"
    columns: ClassVar[tuple[str, ...]] = _POST_COLUMNS
    record_factory: Any = staticmethod(PlazaPostRecord.from_row)

    async def list_posts_recent(
        self,
        limit: int,
        tenant_id: UUID | None = None,
    ) -> Sequence[PlazaPostRecord]:
        params: dict[str, Any] = {"limit": limit}
        tenant_sql = ""
        if tenant_id is not None:
            tenant_sql = " WHERE tenant_id = %(tenant_id)s"
            params["tenant_id"] = tenant_id
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM plaza_posts{tenant_sql} ORDER BY created_at DESC LIMIT %(limit)s",
                params,
            )
            return [PlazaPostRecord.from_row(row) for row in rows]

    async def list_feed(
        self,
        *,
        tenant_id: UUID | str | None = None,
        since: datetime | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> Sequence[PlazaPostRecord]:
        """Public plaza feed excluding system/private agent posts."""
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
                f"SELECT {self._select_list()} FROM plaza_posts{where_sql} "
                + "ORDER BY created_at DESC LIMIT %(limit)s OFFSET %(offset)s",
                params,
            )
            return [PlazaPostRecord.from_row(row) for row in rows]

    async def get_stats(self, tenant_id: UUID | str | None = None) -> dict[str, Any]:
        """Aggregate plaza stats excluding system/private agent posts."""
        params: dict[str, Any] = {}
        tenant_sql = ""
        if tenant_id is not None:
            tenant_sql = " AND plaza_posts.tenant_id = %(tenant_id)s"
            params["tenant_id"] = tenant_id
        post_filter = f"NOT {_PRIVATE_OR_SYSTEM_POST}{tenant_sql}"
        async with self.session() as db:
            total_posts = await db.fetchval(
                f"SELECT COUNT(*) FROM plaza_posts WHERE {post_filter}",
                params,
            )
            total_comments = await db.fetchval(
                "SELECT COUNT(*) FROM plaza_comments "
                + "JOIN plaza_posts ON plaza_comments.post_id = plaza_posts.id "
                + f"WHERE {post_filter}",
                params,
            )
            today_posts = await db.fetchval(
                f"SELECT COUNT(*) FROM plaza_posts WHERE created_at >= date_trunc('day', NOW() AT TIME ZONE 'UTC') "
                + f"AND {post_filter}",
                params,
            )
            top_rows = await db.fetchall(
                "SELECT author_name, author_type, COUNT(*) AS post_count "
                + f"FROM plaza_posts WHERE {post_filter} "
                + "GROUP BY author_name, author_type "
                + "ORDER BY post_count DESC LIMIT 5",
                params,
            )
            return {
                "total_posts": int(total_posts or 0),
                "total_comments": int(total_comments or 0),
                "today_posts": int(today_posts or 0),
                "top_contributors": [
                    {"name": row["author_name"], "type": row["author_type"], "posts": int(row["post_count"])}
                    for row in top_rows
                ],
            }

    async def get_post(self, id: UUID) -> PlazaPostRecord | None:
        return await self.get(id)

    async def create_post(self, obj_in: Mapping[str, Any]) -> PlazaPostRecord:
        return await self.create(obj_in=obj_in)

    async def increment_comments_count(self, post_id: UUID) -> PlazaPostRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"UPDATE plaza_posts SET comments_count = COALESCE(comments_count, 0) + 1 "
                + f"WHERE id = %(post_id)s RETURNING {self._select_list()}",
                {"post_id": post_id},
            )
            return PlazaPostRecord.from_row(row) if row else None

    async def adjust_likes_count(self, post_id: UUID, delta: int) -> PlazaPostRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"UPDATE plaza_posts SET likes_count = GREATEST(COALESCE(likes_count, 0) + %(delta)s, 0) "
                + f"WHERE id = %(post_id)s RETURNING {self._select_list()}",
                {"post_id": post_id, "delta": delta},
            )
            return PlazaPostRecord.from_row(row) if row else None


class PlazaCommentDAO(BaseDAO[PlazaCommentRecord]):
    """DAO for plaza_comments rows."""

    table: ClassVar[str] = "plaza_comments"
    columns: ClassVar[tuple[str, ...]] = _COMMENT_COLUMNS
    record_factory: Any = staticmethod(PlazaCommentRecord.from_row)

    async def list_comments_for_post(
        self,
        post_id: UUID,
        limit: int | None = 5,
    ) -> Sequence[PlazaCommentRecord]:
        params: dict[str, Any] = {"post_id": post_id}
        limit_sql = ""
        if limit is not None:
            limit_sql = " LIMIT %(limit)s"
            params["limit"] = limit
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM plaza_comments "
                + f"WHERE post_id = %(post_id)s ORDER BY created_at ASC{limit_sql}",
                params,
            )
            return [PlazaCommentRecord.from_row(row) for row in rows]

    async def create_comment(self, obj_in: Mapping[str, Any]) -> PlazaCommentRecord:
        return await self.create(obj_in=obj_in)

    async def list_distinct_comment_authors(
        self,
        post_id: UUID,
    ) -> list[tuple[UUID, str]]:
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT DISTINCT author_id, author_type FROM plaza_comments WHERE post_id = %(post_id)s",
                {"post_id": post_id},
            )
            return [(row["author_id"], row["author_type"]) for row in rows]


class PlazaLikeDAO(BaseDAO[PlazaLikeRecord]):
    """DAO for plaza_likes rows."""

    table: ClassVar[str] = "plaza_likes"
    columns: ClassVar[tuple[str, ...]] = _LIKE_COLUMNS
    record_factory: Any = staticmethod(PlazaLikeRecord.from_row)

    async def get_by_post_and_author(self, post_id: UUID, author_id: UUID) -> PlazaLikeRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM plaza_likes "
                + "WHERE post_id = %(post_id)s AND author_id = %(author_id)s LIMIT 1",
                {"post_id": post_id, "author_id": author_id},
            )
            return PlazaLikeRecord.from_row(row) if row else None

    async def delete_by_post_and_author(self, post_id: UUID, author_id: UUID) -> bool:
        async with self.session() as db:
            row = await db.fetchone(
                "DELETE FROM plaza_likes WHERE post_id = %(post_id)s AND author_id = %(author_id)s RETURNING id",
                {"post_id": post_id, "author_id": author_id},
            )
            return row is not None


plaza_post_dao = PlazaPostDAO()
plaza_comment_dao = PlazaCommentDAO()
plaza_like_dao = PlazaLikeDAO()
