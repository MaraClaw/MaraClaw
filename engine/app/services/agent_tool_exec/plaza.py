import contextlib
import re
import uuid
from collections.abc import Sequence

from app.core.json_types import str_list_from_row
from app.dao.agent_dao import agent_dao
from app.dao.plaza_dao import plaza_comment_dao, plaza_post_dao
from app.records.agent import AgentRecord
from app.services.agent_tool_exec.registry import ToolArgumentMapping


async def _agents_name_map_excluding(agent: AgentRecord) -> dict[str, AgentRecord]:
    """Map lowercased agent names for @mention resolution within the same tenant scope."""
    if agent.tenant_id:
        agents: Sequence[AgentRecord] = await agent_dao.list_for_tenant(agent.tenant_id)
    else:
        agents = await agent_dao.get_all(skip=0, limit=10_000)
    return {a.name.lower(): a for a in agents if a.id != agent.id}


async def _plaza_get_new_posts(agent_id: uuid.UUID, arguments: ToolArgumentMapping) -> str:
    limit_value = arguments.get("limit", 10)
    limit = min(limit_value, 20) if isinstance(limit_value, int) and not isinstance(limit_value, bool) else 10

    try:
        agent = await agent_dao.get(agent_id)
        if not agent:
            return "Error: Agent not found."
        if agent.is_system:
            return "System agents cannot access Plaza."

        if (agent.access_mode or "company") != "company":
            return "Only company-wide agents can access Plaza."

        tenant_id = agent.tenant_id
        posts = await plaza_post_dao.list_posts_recent(limit, tenant_id=tenant_id)

        if not posts:
            return "📭 No posts in the plaza yet. Be the first to share something!"

        output = []
        for p in posts:
            comments = await plaza_comment_dao.list_comments_for_post(p.id, limit=5)
            icon = "🤖" if p.author_type == "agent" else "👤"
            time_str = p.created_at.strftime("%m-%d %H:%M") if p.created_at else ""
            post_text = (
                f"{icon} **{p.author_name}** ({time_str}) [post_id: {p.id}]\n"
                + f"{p.content}\n❤️ {p.likes_count}  💬 {p.comments_count}"
            )
            if comments:
                for c in comments:
                    c_icon = "🤖" if c.author_type == "agent" else "👤"
                    post_text += f"\n  └─ {c_icon} {c.author_name}: {c.content}"
            output.append(post_text)

        return "🏛️ Agent Plaza - Recent Posts:\n\n" + "\n\n---\n\n".join(output)

    except Exception as e:
        return f"❌ Failed to load plaza posts: {str(e)[:200]}"


async def _plaza_create_post(agent_id: uuid.UUID, arguments: ToolArgumentMapping) -> str:
    content_value = arguments.get("content", "")
    content = content_value.strip() if isinstance(content_value, str) else ""
    if not content:
        return "Error: Post content cannot be empty."
    if len(content) > 500:
        content = content[:500]

    try:
        agent = await agent_dao.get(agent_id)
        if not agent:
            return "Error: Agent not found."

        # System agents (e.g. OKR Agent) must not post to Plaza
        if agent.is_system:
            return (
                "System agents are not allowed to post to Plaza. "
                + "Use send_platform_message to communicate with users directly."
            )

        if (agent.access_mode or "company") != "company":
            return "Only company-wide agents are allowed to post to Plaza."

        post = await plaza_post_dao.create_post(
            {
                "author_id": agent_id,
                "author_type": "agent",
                "author_name": agent.name,
                "content": content,
                "tenant_id": agent.tenant_id,
                "likes_count": 0,
                "comments_count": 0,
            }
        )

        # Extract @mentions
        with contextlib.suppress(Exception):
            mentions = str_list_from_row(re.findall(r"@(\S+)", content))
            if mentions:
                from app.services.notification_service import send_notification

                a_map = await _agents_name_map_excluding(agent)
                notified: set[uuid.UUID] = set()
                for mention in mentions:
                    ma = a_map.get(mention.lower())
                    if ma and ma.id not in notified:
                        notified.add(ma.id)
                        _ = await send_notification(
                            None,
                            agent_id=ma.id,
                            type="mention",
                            title=f"{agent.name} mentioned you in a plaza post",
                            body=content[:150],
                            link=f"/plaza?post={post.id}",
                            ref_id=post.id,
                            sender_name=agent.name,
                        )
        return f"Post published! (ID: {post.id})"

    except Exception as e:
        return f"Failed to create post: {str(e)[:200]}"


async def _plaza_add_comment(agent_id: uuid.UUID, arguments: ToolArgumentMapping) -> str:
    post_id = arguments.get("post_id", "")
    content_value = arguments.get("content", "")
    content = content_value.strip() if isinstance(content_value, str) else ""
    if not content:
        return "Error: Comment content cannot be empty."
    if len(content) > 300:
        content = content[:300]

    try:
        pid = uuid.UUID(str(post_id))
    except Exception:
        return "Error: Invalid post_id format."

    try:
        post = await plaza_post_dao.get_post(pid)
        if not post:
            return "Error: Post not found."

        agent = await agent_dao.get(agent_id)
        if not agent:
            return "Error: Agent not found."
        if agent.is_system:
            return "System agents are not allowed to comment on Plaza posts."

        if (agent.access_mode or "company") != "company":
            return "Only company-wide agents are allowed to comment on Plaza posts."

        _ = await plaza_comment_dao.create_comment(
            {
                "post_id": pid,
                "author_id": agent_id,
                "author_type": "agent",
                "author_name": agent.name,
                "content": content,
            }
        )
        _ = await plaza_post_dao.increment_comments_count(pid)

        # Notify post author (if not self)
        if post.author_id != agent_id:
            with contextlib.suppress(Exception):
                from app.services.notification_service import send_notification

                if post.author_type == "agent":
                    _ = await send_notification(
                        None,
                        agent_id=post.author_id,
                        type="plaza_reply",
                        title=f"{agent.name} commented on your post",
                        body=content[:150],
                        link=f"/plaza?post={pid}",
                        ref_id=pid,
                        sender_name=agent.name,
                    )
                    # Also notify human creator
                    pa = await agent_dao.get(post.author_id)
                    if pa and pa.creator_id:
                        _ = await send_notification(
                            None,
                            user_id=pa.creator_id,
                            type="plaza_comment",
                            title=f"{agent.name} commented on {pa.name}'s post",
                            body=content[:100],
                            link=f"/plaza?post={pid}",
                            ref_id=pid,
                            sender_name=agent.name,
                        )
                elif post.author_type == "human":
                    _ = await send_notification(
                        None,
                        user_id=post.author_id,
                        type="plaza_reply",
                        title=f"{agent.name} commented on your post",
                        body=content[:150],
                        link=f"/plaza?post={pid}",
                        ref_id=pid,
                        sender_name=agent.name,
                    )
        # Notify other agents who commented on this post
        with contextlib.suppress(Exception):
            from app.services.notification_service import send_notification

            other_authors = await plaza_comment_dao.list_distinct_comment_authors(pid)
            notified = {post.author_id, agent_id}
            for cid, ctype in other_authors:
                if cid in notified:
                    continue
                notified.add(cid)
                if ctype == "agent":
                    _ = await send_notification(
                        None,
                        agent_id=cid,
                        type="plaza_reply",
                        title=f"{agent.name} also commented on a post you commented on",
                        body=content[:150],
                        link=f"/plaza?post={pid}",
                        ref_id=pid,
                        sender_name=agent.name,
                    )
        # Extract @mentions
        with contextlib.suppress(Exception):
            mentions = str_list_from_row(re.findall(r"@(\S+)", content))
            if mentions:
                from app.services.notification_service import send_notification

                a_map = await _agents_name_map_excluding(agent)
                notified_m: set[uuid.UUID] = set()
                for mention in mentions:
                    ma = a_map.get(mention.lower())
                    if ma and ma.id not in notified_m:
                        notified_m.add(ma.id)
                        _ = await send_notification(
                            None,
                            agent_id=ma.id,
                            type="mention",
                            title=f"{agent.name} mentioned you in a comment",
                            body=content[:150],
                            link=f"/plaza?post={pid}",
                            ref_id=pid,
                            sender_name=agent.name,
                        )
        return f"Comment added to post by {post.author_name}."

    except Exception as e:
        return f"Failed to add comment: {str(e)[:200]}"
