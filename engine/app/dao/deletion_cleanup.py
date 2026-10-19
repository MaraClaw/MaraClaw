from collections.abc import Sequence
from uuid import UUID

from app.core.json_types import uuid_from_row
from app.db.connection import DbConnection, Params


async def execute_cleanup(db: DbConnection, statements: Sequence[str], params: Params) -> None:
    affected_posts: set[UUID] = set()
    for statement in statements:
        if statement.startswith(("DELETE FROM plaza_comments ", "DELETE FROM plaza_likes ")):
            rows = await db.fetchall(statement + " RETURNING post_id", params)
            affected_posts.update(uuid_from_row(row["post_id"]) for row in rows)
        else:
            await db.execute(statement, params)
    if affected_posts:
        await db.execute(
            "UPDATE plaza_posts SET "
            "comments_count = (SELECT COUNT(*) FROM plaza_comments WHERE post_id = plaza_posts.id), "
            "likes_count = (SELECT COUNT(*) FROM plaza_likes WHERE post_id = plaza_posts.id) "
            "WHERE id = ANY(%(post_ids)s)",
            {"post_ids": list(affected_posts)},
        )
