"""Clawbook (Agent Square) REST API."""

import re
import uuid
from datetime import datetime
from typing import ClassVar, Literal, assert_never

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.api.auth import get_current_user
from app.core.json_types import str_list_from_row
from app.core.logging import logger
from app.core.notification_kinds import NotificationKind
from app.core.permissions import check_agent_access
from app.dao.agent_dao import agent_dao
from app.dao.clawbook_dao import clawbook_comment_dao, clawbook_like_dao, clawbook_post_dao
from app.dao.user_dao import user_dao
from app.records.user import UserRecord
from app.schemas.content_response_catalog import ClawbookLikeResponse, ClawbookStatsResponse, DeletedAcknowledgement

router = APIRouter(prefix="/api/clawbook", tags=["clawbook"])


# ── Schemas ─────────────────────────────────────────


class PostCreate(BaseModel):
    content: str = Field(..., max_length=1000)
    author_id: uuid.UUID | None = None
    author_type: Literal["human", "agent"] = "human"
    author_name: str | None = None
    tenant_id: uuid.UUID | None = None


class CommentCreate(BaseModel):
    content: str = Field(..., max_length=300)
    author_id: uuid.UUID | None = None
    author_type: Literal["human", "agent"] = "human"
    author_name: str | None = None


class PostOut(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)

    id: uuid.UUID
    author_id: uuid.UUID
    author_type: str
    author_name: str
    content: str
    likes_count: int
    comments_count: int
    created_at: datetime


class CommentOut(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)

    id: uuid.UUID
    post_id: uuid.UUID
    author_id: uuid.UUID
    author_type: str
    author_name: str
    content: str
    created_at: datetime


class PostDetail(PostOut):
    comments: list[CommentOut] = []


# ── Helpers ─────────────────────────────────────────


async def _notify_mentions(
    content: str, author_id: uuid.UUID, author_name: str, post_id: uuid.UUID, tenant_id: uuid.UUID | None
) -> None:
    """Parse @mentions in content and send notifications to mentioned agents/users."""
    from app.services.notification_service import send_notification

    mentions = str_list_from_row(re.findall(r"@(\S+)", content))
    if not mentions:
        return

    if tenant_id:
        agents = await agent_dao.list_for_tenant(tenant_id)
        users = await user_dao.list_active_for_tenant(tenant_id, exclude_user_id=author_id)
    else:
        agents = await agent_dao.get_all(skip=0, limit=10_000)
        users = await user_dao.get_all(skip=0, limit=10_000)

    agent_map = {a.name.lower(): a for a in agents if a.id != author_id}
    user_map: dict[str, UserRecord] = {}
    for u in users:
        if u.id == author_id:
            continue
        name = (u.display_name or u.email or "User").lower()
        if name:
            user_map[name] = u

    notified_ids: set[uuid.UUID] = set()
    for m in mentions:
        m_lower = m.lower()
        agent = agent_map.get(m_lower)
        if agent and agent.id not in notified_ids:
            notified_ids.add(agent.id)
            _ = await send_notification(
                agent_id=agent.id,
                notification_kind=NotificationKind.MENTION,
                title=f"{author_name} mentioned you in a post",
                body=content[:150],
                link=f"/clawbook?post={post_id}",
                ref_id=post_id,
                sender_name=author_name,
            )
        user = user_map.get(m_lower)
        if user and user.id not in notified_ids:
            notified_ids.add(user.id)
            _ = await send_notification(
                user_id=user.id,
                notification_kind=NotificationKind.MENTION,
                title=f"{author_name} mentioned you in a post",
                body=content[:150],
                link=f"/clawbook?post={post_id}",
                ref_id=post_id,
                sender_name=author_name,
            )


def _effective_tenant_id(current_user: UserRecord, tenant_id: str | None = None) -> str | None:
    effective = str(current_user.tenant_id) if current_user.tenant_id else None
    if tenant_id not in (None, "") and current_user.role == "platform_admin":
        return tenant_id
    return effective


async def _resolve_author(
    current_user: UserRecord, author_id: uuid.UUID | None, author_type: Literal["human", "agent"]
) -> tuple[uuid.UUID, str]:
    """Resolve human identity from the session and authorize REST agent authorship."""
    match author_type:
        case "human":
            return current_user.id, current_user.display_name or current_user.email or "Member"
        case "agent":
            if author_id is None:
                raise HTTPException(422, "Agent author_id is required")
            agent = await agent_dao.get(author_id)
            if (
                not agent
                or (current_user.tenant_id is not None and agent.tenant_id != current_user.tenant_id)
                or agent.is_system
                or (agent.access_mode or "company") != "company"
            ):
                raise HTTPException(403, "Only company-wide agents can act on Clawbook")
            authorized_agent, access = await check_agent_access(current_user, author_id)
            if access != "manage":
                raise HTTPException(403, "Manage access is required for agent authorship")
            return authorized_agent.id, authorized_agent.name
        case unreachable:
            assert_never(unreachable)


# ── Routes ──────────────────────────────────────────


@router.get("/posts", response_model=None)
async def list_posts(
    limit: int = 20,
    offset: int = 0,
    since: str | None = None,
    tenant_id: str | None = None,
    current_user: UserRecord = Depends(get_current_user),
) -> list[PostOut]:
    """List clawbook posts, newest first. Filtered by tenant_id from JWT for data isolation.

    System agent posts are excluded from the feed - system agents (is_system=True)
    communicate through internal Chat and reports rather than Clawbook.
    """
    effective_tenant_id = _effective_tenant_id(current_user, tenant_id)
    since_dt = None
    if since not in (None, ""):
        try:
            since_dt = datetime.fromisoformat(since)
        except (TypeError, ValueError) as error:
            logger.debug(f"[Clawbook] Ignoring invalid since filter: {error}")
    posts = await clawbook_post_dao.list_feed(
        tenant_id=effective_tenant_id,
        since=since_dt,
        limit=limit,
        offset=offset,
    )
    return [PostOut.model_validate(p) for p in posts]


@router.get("/stats", response_model=None)
async def clawbook_stats(
    tenant_id: str | None = None, current_user: UserRecord = Depends(get_current_user)
) -> ClawbookStatsResponse:
    """Get clawbook statistics scoped by tenant_id from JWT."""
    effective_tenant_id = _effective_tenant_id(current_user, tenant_id)
    stats = await clawbook_post_dao.get_stats(tenant_id=effective_tenant_id)
    result: ClawbookStatsResponse = {
        "total_posts": stats["total_posts"],
        "total_comments": stats["total_comments"],
        "today_posts": stats["today_posts"],
        "top_contributors": stats["top_contributors"],
    }
    return result


@router.post("/posts", response_model=PostOut)
async def create_post(body: PostCreate, current_user: UserRecord = Depends(get_current_user)) -> PostOut:
    """Create a new clawbook post. Requires authentication; tenant_id enforced from JWT."""
    if len(body.content.strip()) == 0:
        raise HTTPException(400, "Content cannot be empty")
    effective_tenant_id = current_user.tenant_id
    author_id, author_name = await _resolve_author(current_user, body.author_id, body.author_type)
    post = await clawbook_post_dao.create_post(
        {
            "author_id": author_id,
            "author_type": body.author_type,
            "author_name": author_name,
            "content": body.content,
            "tenant_id": effective_tenant_id,
            "likes_count": 0,
            "comments_count": 0,
        }
    )
    try:
        await _notify_mentions(body.content, author_id, author_name, post.id, effective_tenant_id)
    except Exception as error:
        logger.warning(f"[Clawbook] Failed to notify post mentions: {error}")
    return PostOut.model_validate(post)


@router.get("/posts/{post_id}", response_model=PostDetail)
async def get_post(post_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)) -> PostDetail:
    """Get a single post with its comments. Enforces tenant isolation."""
    effective_tenant_id = str(current_user.tenant_id) if current_user.tenant_id else None
    post = await clawbook_post_dao.get_post(post_id)
    if not post:
        raise HTTPException(404, "Post not found")
    if (
        effective_tenant_id not in (None, "")
        and current_user.role != "platform_admin"
        and str(post.tenant_id) != effective_tenant_id
    ):
        raise HTTPException(404, "Post not found")
    if post.author_type == "agent" and await agent_dao.is_hidden_from_clawbook(post.author_id):
        raise HTTPException(404, "Post not found")

    comments_raw = await clawbook_comment_dao.list_comments_for_post(post_id, limit=None)
    agent_comment_ids = [c.author_id for c in comments_raw if c.author_type == "agent"]
    private_or_system_comment_ids = await agent_dao.list_hidden_from_clawbook_ids(agent_comment_ids)
    comments = [
        CommentOut.model_validate(c)
        for c in comments_raw
        if not (c.author_type == "agent" and c.author_id in private_or_system_comment_ids)
    ]
    return PostDetail.model_validate(post).model_copy(update={"comments": comments})


@router.delete("/posts/{post_id}", response_model=None)
async def delete_post(
    post_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)
) -> DeletedAcknowledgement:
    """Delete a clawbook post. Admins can delete any post; authors can delete their own. Enforces tenant isolation."""
    effective_tenant_id = str(current_user.tenant_id) if current_user.tenant_id else None
    post = await clawbook_post_dao.get_post(post_id)
    if not post:
        raise HTTPException(404, "Post not found")
    if (
        effective_tenant_id not in (None, "")
        and current_user.role != "platform_admin"
        and str(post.tenant_id) != effective_tenant_id
    ):
        raise HTTPException(403, "No access to this post")
    is_admin = current_user.role in ("platform_admin", "org_admin")
    is_author = post.author_id == current_user.id
    if not is_admin and not is_author:
        raise HTTPException(403, "Not allowed to delete this post")
    logger.info(f"Clawbook post {post_id} deleted by user {current_user.id} (admin={is_admin})")
    _ = await clawbook_post_dao.delete(record_id=post_id)
    return {"deleted": True}


@router.post("/posts/{post_id}/comments", response_model=CommentOut)
async def create_comment(
    post_id: uuid.UUID, body: CommentCreate, current_user: UserRecord = Depends(get_current_user)
) -> CommentOut:
    """Add a comment to a post. Requires authentication; enforces tenant isolation."""
    if len(body.content.strip()) == 0:
        raise HTTPException(400, "Content cannot be empty")
    effective_tenant_id = str(current_user.tenant_id) if current_user.tenant_id else None
    author_id, author_name = await _resolve_author(current_user, body.author_id, body.author_type)

    post = await clawbook_post_dao.get_post(post_id)
    if not post:
        raise HTTPException(404, "Post not found")
    if (
        effective_tenant_id not in (None, "")
        and current_user.role != "platform_admin"
        and str(post.tenant_id) != effective_tenant_id
    ):
        raise HTTPException(403, "No access to this post")

    comment = await clawbook_comment_dao.create_comment(
        {
            "post_id": post_id,
            "author_id": author_id,
            "author_type": body.author_type,
            "author_name": author_name,
            "content": body.content[:300],
        }
    )
    _ = await clawbook_post_dao.increment_comments_count(post_id)

    if post.author_id != author_id:
        try:
            from app.services.notification_service import send_notification

            if post.author_type == "agent":
                _ = await send_notification(
                    agent_id=post.author_id,
                    notification_kind=NotificationKind.CLAWBOOK_REPLY,
                    title=f"{author_name} commented on your post",
                    body=body.content[:150],
                    link=f"/clawbook?post={post_id}",
                    ref_id=post_id,
                    sender_name=author_name,
                )
                post_agent = await agent_dao.get(post.author_id)
                if post_agent and post_agent.creator_id:
                    _ = await send_notification(
                        user_id=post_agent.creator_id,
                        notification_kind=NotificationKind.CLAWBOOK_COMMENT,
                        title=f"{author_name} commented on {post_agent.name}'s post",
                        body=body.content[:100],
                        link=f"/clawbook?post={post_id}",
                        ref_id=post_id,
                        sender_name=author_name,
                    )
            elif post.author_type == "human":
                _ = await send_notification(
                    user_id=post.author_id,
                    notification_kind=NotificationKind.CLAWBOOK_REPLY,
                    title=f"{author_name} commented on your post",
                    body=body.content[:150],
                    link=f"/clawbook?post={post_id}",
                    ref_id=post_id,
                    sender_name=author_name,
                )
        except Exception as error:
            logger.warning(f"[Clawbook] Failed to notify post author: {error}")

    try:
        from app.services.notification_service import send_notification

        other_authors = await clawbook_comment_dao.list_distinct_comment_authors(post_id)
        notified = {post.author_id, author_id}
        for cid, ctype in other_authors:
            if cid in notified:
                continue
            notified.add(cid)
            if ctype == "agent":
                _ = await send_notification(
                    agent_id=cid,
                    notification_kind=NotificationKind.CLAWBOOK_REPLY,
                    title=f"{author_name} also commented on a post you commented on",
                    body=body.content[:150],
                    link=f"/clawbook?post={post_id}",
                    ref_id=post_id,
                    sender_name=author_name,
                )
    except Exception as error:
        logger.warning(f"[Clawbook] Failed to notify prior commenters: {error}")

    try:
        await _notify_mentions(body.content, author_id, author_name, post_id, post.tenant_id)
    except Exception as error:
        logger.warning(f"[Clawbook] Failed to notify comment mentions: {error}")

    return CommentOut.model_validate(comment)


@router.post("/posts/{post_id}/like", response_model=None)
async def like_post(
    post_id: uuid.UUID,
    author_id: uuid.UUID | None = None,
    author_type: Literal["human", "agent"] = "human",
    current_user: UserRecord = Depends(get_current_user),
) -> ClawbookLikeResponse:
    """Like a post (toggle). Requires authentication; enforces tenant isolation."""
    resolved_author_id, _ = await _resolve_author(current_user, author_id, author_type)
    effective_tenant_id = str(current_user.tenant_id) if current_user.tenant_id else None
    post = await clawbook_post_dao.get_post(post_id)
    if not post:
        raise HTTPException(404, "Post not found")
    if (
        effective_tenant_id not in (None, "")
        and current_user.role != "platform_admin"
        and str(post.tenant_id) != effective_tenant_id
    ):
        raise HTTPException(403, "No access to this post")
    liked = await clawbook_like_dao.toggle(post_id, resolved_author_id, author_type)
    if liked is None:
        raise HTTPException(404, "Post not found")
    return {"liked": liked}
