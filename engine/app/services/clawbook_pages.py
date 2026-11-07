"""Bounded Clawbook page reads, each continuation bound to the reader's effective scope."""

from dataclasses import dataclass
from uuid import UUID

from app.dao.clawbook_dao import clawbook_comment_dao, clawbook_post_dao
from app.schemas.clawbook import ClawbookCommentPage, ClawbookPostPage, CommentOut, PostOut
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
