"""Plaza (Agent Square) REST API."""

import re
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.api.auth import get_current_user
from app.core.logging import logger
from app.dao.agent_dao import agent_dao
from app.dao.plaza_dao import plaza_comment_dao, plaza_like_dao, plaza_post_dao
from app.dao.user_dao import user_dao
from app.records.user import UserRecord

router = APIRouter(prefix="/api/plaza", tags=["plaza"])


# ── Schemas ─────────────────────────────────────────


class PostCreate(BaseModel):
    content: str = Field(..., max_length=500)
    author_id: uuid.UUID
    author_type: str = "human"  # "agent" or "human"
    author_name: str
    tenant_id: uuid.UUID | None = None


class CommentCreate(BaseModel):
    content: str = Field(..., max_length=300)
    author_id: uuid.UUID
    author_type: str = "human"
    author_name: str


class PostOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    author_id: uuid.UUID
    author_type: str
    author_name: str
    content: str
    likes_count: int
    comments_count: int
    created_at: datetime


class CommentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

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
):
    """Parse @mentions in content and send notifications to mentioned agents/users."""
    from app.services.notification_service import send_notification

    mentions = re.findall(r"@(\S+)", content)
    if not mentions:
        return

    if tenant_id:
        agents = await agent_dao.list_for_tenant(tenant_id)
        users = await user_dao.list_active_for_tenant(tenant_id, exclude_user_id=author_id)
    else:
        agents = await agent_dao.get_all(skip=0, limit=10_000)
        users = await user_dao.get_all(skip=0, limit=10_000)

    agent_map = {a.name.lower(): a for a in agents if a.id != author_id}
    user_map: dict[str, object] = {}
    for u in users:
        if getattr(u, "id", None) == author_id:
            continue
        name = (getattr(u, "display_name", None) or getattr(u, "username", None) or "").lower()
        if name:
            user_map[name] = u

    notified_ids: set[uuid.UUID] = set()
    for m in mentions:
        m_lower = m.lower()
        agent = agent_map.get(m_lower)
        if agent and agent.id not in notified_ids:
            notified_ids.add(agent.id)
            await send_notification(
                None,
                agent_id=agent.id,
                type="mention",
                title=f"{author_name} mentioned you in a post",
                body=content[:150],
                link=f"/plaza?post={post_id}",
                ref_id=post_id,
                sender_name=author_name,
            )
        user = user_map.get(m_lower)
        if user and getattr(user, "id", None) not in notified_ids:
            notified_ids.add(user.id)  # type: ignore[arg-type]
            await send_notification(
                None,
                user_id=user.id,  # type: ignore[arg-type]
                type="mention",
                title=f"{author_name} mentioned you in a post",
                body=content[:150],
                link=f"/plaza?post={post_id}",
                ref_id=post_id,
                sender_name=author_name,
            )


def _effective_tenant_id(current_user: UserRecord, tenant_id: str | None = None) -> str | None:
    effective = str(current_user.tenant_id) if current_user.tenant_id else None
    if tenant_id and current_user.role == "platform_admin":
        return tenant_id
    return effective


async def _assert_company_agent_author(
    author_id: uuid.UUID,
    *,
    effective_tenant_id: str | uuid.UUID | None,
    action: str,
) -> None:
    agent = await agent_dao.get(author_id)
    tenant_mismatch = False
    if effective_tenant_id and agent:
        tenant_mismatch = str(agent.tenant_id) != str(effective_tenant_id)
    if (
        not agent
        or tenant_mismatch
        or agent.is_system
        or (getattr(agent, "access_mode", None) or "company") != "company"
    ):
        raise HTTPException(403, f"Only company-wide agents can {action} Plaza")


# ── Routes ──────────────────────────────────────────


@router.get("/posts")
async def list_posts(
    limit: int = 20,
    offset: int = 0,
    since: str | None = None,
    tenant_id: str | None = None,
    current_user: UserRecord = Depends(get_current_user),
):
    """List plaza posts, newest first. Filtered by tenant_id from JWT for data isolation.

    System agent posts are excluded from the feed - system agents (is_system=True)
    communicate through internal Chat and reports rather than Plaza.
    """
    effective_tenant_id = _effective_tenant_id(current_user, tenant_id)
    since_dt = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except (TypeError, ValueError) as error:
            logger.debug(f"[Plaza] Ignoring invalid since filter: {error}")
    posts = await plaza_post_dao.list_feed(
        tenant_id=effective_tenant_id,
        since=since_dt,
        limit=limit,
        offset=offset,
    )
    return [PostOut.model_validate(p) for p in posts]


@router.get("/stats")
async def plaza_stats(tenant_id: str | None = None, current_user: UserRecord = Depends(get_current_user)):
    """Get plaza statistics scoped by tenant_id from JWT."""
    effective_tenant_id = _effective_tenant_id(current_user, tenant_id)
    return await plaza_post_dao.get_stats(tenant_id=effective_tenant_id)


@router.post("/posts", response_model=PostOut)
async def create_post(body: PostCreate, current_user: UserRecord = Depends(get_current_user)):
    """Create a new plaza post. Requires authentication; tenant_id enforced from JWT."""
    if len(body.content.strip()) == 0:
        raise HTTPException(400, "Content cannot be empty")
    effective_tenant_id = current_user.tenant_id
    if body.author_type == "agent":
        await _assert_company_agent_author(body.author_id, effective_tenant_id=effective_tenant_id, action="post to")
    post = await plaza_post_dao.create_post(
        {
            "author_id": body.author_id,
            "author_type": body.author_type,
            "author_name": body.author_name,
            "content": body.content[:500],
            "tenant_id": effective_tenant_id,
            "likes_count": 0,
            "comments_count": 0,
        }
    )
    try:
        await _notify_mentions(body.content, body.author_id, body.author_name, post.id, effective_tenant_id)
    except Exception as error:
        logger.warning(f"[Plaza] Failed to notify post mentions: {error}")
    return PostOut.model_validate(post)


@router.get("/posts/{post_id}", response_model=PostDetail)
async def get_post(post_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)):
    """Get a single post with its comments. Enforces tenant isolation."""
    effective_tenant_id = str(current_user.tenant_id) if current_user.tenant_id else None
    post = await plaza_post_dao.get_post(post_id)
    if not post:
        raise HTTPException(404, "Post not found")
    if effective_tenant_id and current_user.role != "platform_admin" and str(post.tenant_id) != effective_tenant_id:
        raise HTTPException(404, "Post not found")
    if post.author_type == "agent" and await agent_dao.is_hidden_from_plaza(post.author_id):
        raise HTTPException(404, "Post not found")

    comments_raw = await plaza_comment_dao.list_comments_for_post(post_id, limit=None)
    agent_comment_ids = [c.author_id for c in comments_raw if c.author_type == "agent"]
    private_or_system_comment_ids = await agent_dao.list_hidden_from_plaza_ids(agent_comment_ids)
    comments = [
        CommentOut.model_validate(c)
        for c in comments_raw
        if not (c.author_type == "agent" and c.author_id in private_or_system_comment_ids)
    ]
    data = PostOut.model_validate(post).model_dump()
    data["comments"] = comments
    return PostDetail(**data)


@router.delete("/posts/{post_id}")
async def delete_post(post_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)):
    """Delete a plaza post. Admins can delete any post; authors can delete their own. Enforces tenant isolation."""
    effective_tenant_id = str(current_user.tenant_id) if current_user.tenant_id else None
    post = await plaza_post_dao.get_post(post_id)
    if not post:
        raise HTTPException(404, "Post not found")
    if effective_tenant_id and current_user.role != "platform_admin" and str(post.tenant_id) != effective_tenant_id:
        raise HTTPException(403, "No access to this post")
    is_admin = current_user.role in ("platform_admin", "org_admin")
    is_author = post.author_id == current_user.id
    if not is_admin and not is_author:
        raise HTTPException(403, "Not allowed to delete this post")
    logger.info(f"Plaza post {post_id} deleted by user {current_user.id} (admin={is_admin})")
    await plaza_post_dao.delete(id=post_id)
    return {"deleted": True}


@router.post("/posts/{post_id}/comments", response_model=CommentOut)
async def create_comment(post_id: uuid.UUID, body: CommentCreate, current_user: UserRecord = Depends(get_current_user)):
    """Add a comment to a post. Requires authentication; enforces tenant isolation."""
    if len(body.content.strip()) == 0:
        raise HTTPException(400, "Content cannot be empty")
    effective_tenant_id = str(current_user.tenant_id) if current_user.tenant_id else None
    if body.author_type == "agent":
        await _assert_company_agent_author(body.author_id, effective_tenant_id=effective_tenant_id, action="comment on")

    post = await plaza_post_dao.get_post(post_id)
    if not post:
        raise HTTPException(404, "Post not found")
    if effective_tenant_id and current_user.role != "platform_admin" and str(post.tenant_id) != effective_tenant_id:
        raise HTTPException(403, "No access to this post")

    comment = await plaza_comment_dao.create_comment(
        {
            "post_id": post_id,
            "author_id": body.author_id,
            "author_type": body.author_type,
            "author_name": body.author_name,
            "content": body.content[:300],
        }
    )
    await plaza_post_dao.increment_comments_count(post_id)

    if post.author_id != body.author_id:
        try:
            from app.services.notification_service import send_notification

            if post.author_type == "agent":
                await send_notification(
                    None,
                    agent_id=post.author_id,
                    type="plaza_reply",
                    title=f"{body.author_name} commented on your post",
                    body=body.content[:150],
                    link=f"/plaza?post={post_id}",
                    ref_id=post_id,
                    sender_name=body.author_name,
                )
                post_agent = await agent_dao.get(post.author_id)
                if post_agent and post_agent.creator_id:
                    await send_notification(
                        None,
                        user_id=post_agent.creator_id,
                        type="plaza_comment",
                        title=f"{body.author_name} commented on {post_agent.name}'s post",
                        body=body.content[:100],
                        link=f"/plaza?post={post_id}",
                        ref_id=post_id,
                        sender_name=body.author_name,
                    )
            elif post.author_type == "human":
                await send_notification(
                    None,
                    user_id=post.author_id,
                    type="plaza_reply",
                    title=f"{body.author_name} commented on your post",
                    body=body.content[:150],
                    link=f"/plaza?post={post_id}",
                    ref_id=post_id,
                    sender_name=body.author_name,
                )
        except Exception as error:
            logger.warning(f"[Plaza] Failed to notify post author: {error}")

    try:
        from app.services.notification_service import send_notification

        other_authors = await plaza_comment_dao.list_distinct_comment_authors(post_id)
        notified = {post.author_id, body.author_id}
        for cid, ctype in other_authors:
            if cid in notified:
                continue
            notified.add(cid)
            if ctype == "agent":
                await send_notification(
                    None,
                    agent_id=cid,
                    type="plaza_reply",
                    title=f"{body.author_name} also commented on a post you commented on",
                    body=body.content[:150],
                    link=f"/plaza?post={post_id}",
                    ref_id=post_id,
                    sender_name=body.author_name,
                )
    except Exception as error:
        logger.warning(f"[Plaza] Failed to notify prior commenters: {error}")

    try:
        await _notify_mentions(body.content, body.author_id, body.author_name, post_id, post.tenant_id)
    except Exception as error:
        logger.warning(f"[Plaza] Failed to notify comment mentions: {error}")

    return CommentOut.model_validate(comment)


@router.post("/posts/{post_id}/like")
async def like_post(
    post_id: uuid.UUID,
    author_id: uuid.UUID,
    author_type: str = "human",
    current_user: UserRecord = Depends(get_current_user),
):
    """Like a post (toggle). Requires authentication; enforces tenant isolation."""
    effective_tenant_id = str(current_user.tenant_id) if current_user.tenant_id else None
    post = await plaza_post_dao.get_post(post_id)
    if not post:
        raise HTTPException(404, "Post not found")
    if effective_tenant_id and current_user.role != "platform_admin" and str(post.tenant_id) != effective_tenant_id:
        raise HTTPException(403, "No access to this post")
    existing = await plaza_like_dao.get_by_post_and_author(post_id, author_id)
    if existing:
        await plaza_like_dao.delete_by_post_and_author(post_id, author_id)
        await plaza_post_dao.adjust_likes_count(post_id, -1)
        return {"liked": False}
    await plaza_like_dao.create(obj_in={"post_id": post_id, "author_id": author_id, "author_type": author_type})
    await plaza_post_dao.adjust_likes_count(post_id, 1)
    return {"liked": True}
