"""Bounded Clawbook page reads, each continuation bound to the reader's effective scope."""

from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException

from app.dao.clawbook_dao import clawbook_comment_dao, clawbook_post_dao
from app.schemas.clawbook import (
    ClawbookAdminComment,
    ClawbookAdminCommentPage,
    ClawbookAdminPost,
    ClawbookAdminPostPage,
    ClawbookCommentPage,
    ClawbookPostPage,
    CommentOut,
    PostOut,
)
from app.services.clawbook_cursor import ClawbookCursor, parse_clawbook_cursor


@dataclass(frozen=True, slots=True)
class PageRequest:
    tenant_id: UUID | None
    limit: int
    cursor: str | None = None


async def read_post_page(request: PageRequest) -> ClawbookPostPage:
    position = parse_clawbook_cursor(request.cursor, request.tenant_id)
    rows = await clawbook_post_dao.list_feed_page(tenant_id=request.tenant_id, before=position, limit=request.limit + 1)
    items = [PostOut.model_validate(row) for row in rows[: request.limit]]
    next_cursor = None
    if len(rows) > request.limit:
        last = items[-1]
        next_cursor = ClawbookCursor(
            tenant_id=request.tenant_id, post_id=None, created_at=last.created_at, id=last.id
        ).encode()
    return ClawbookPostPage(items=items, next_cursor=next_cursor)


async def read_comment_page(post_id: UUID, request: PageRequest) -> ClawbookCommentPage:
    position = parse_clawbook_cursor(request.cursor, request.tenant_id, post_id)
    rows = await clawbook_comment_dao.list_comments_for_post(
        post_id, limit=request.limit + 1, visible_only=True, after=position
    )
    items = [CommentOut.model_validate(row) for row in rows[: request.limit]]
    next_cursor = None
    if len(rows) > request.limit:
        last = items[-1]
        next_cursor = ClawbookCursor(
            tenant_id=request.tenant_id, post_id=post_id, created_at=last.created_at, id=last.id
        ).encode()
    return ClawbookCommentPage(items=items, next_cursor=next_cursor)


async def read_admin_post_page(tenant_id: UUID, *, limit: int, cursor: str | None = None) -> ClawbookAdminPostPage:
    """Every post of one tenant, newest first, each flagged when the member feed omits it."""
    position = parse_clawbook_cursor(cursor, tenant_id, scope="admin")
    rows = await clawbook_post_dao.list_admin_feed_page(tenant_id=tenant_id, before=position, limit=limit + 1)
    items = [
        ClawbookAdminPost.model_validate(
            {**PostOut.model_validate(row.post).model_dump(), "hidden_from_members": row.hidden_from_members}
        )
        for row in rows[:limit]
    ]
    next_cursor = None
    if len(rows) > limit:
        last = items[-1]
        next_cursor = ClawbookCursor(
            scope="admin", tenant_id=tenant_id, post_id=None, created_at=last.created_at, id=last.id
        ).encode()
    return ClawbookAdminPostPage(items=items, next_cursor=next_cursor)


async def read_admin_comment_page(
    tenant_id: UUID, post_id: UUID, *, limit: int, cursor: str | None = None
) -> ClawbookAdminCommentPage:
    """Every comment of one tenant post, oldest first, each flagged when the member feed omits it."""
    post = await clawbook_post_dao.get_post(post_id)
    if post is None or post.tenant_id != tenant_id:
        raise HTTPException(404, "Post not found")
    position = parse_clawbook_cursor(cursor, tenant_id, post_id, scope="admin")
    rows = await clawbook_comment_dao.list_admin_comments_page(post_id, after=position, limit=limit + 1)
    items = [
        ClawbookAdminComment.model_validate(
            {**CommentOut.model_validate(row.comment).model_dump(), "hidden_from_members": row.hidden_from_members}
        )
        for row in rows[:limit]
    ]
    next_cursor = None
    if len(rows) > limit:
        last = items[-1]
        next_cursor = ClawbookCursor(
            scope="admin", tenant_id=tenant_id, post_id=post_id, created_at=last.created_at, id=last.id
        ).encode()
    return ClawbookAdminCommentPage(items=items, next_cursor=next_cursor)
