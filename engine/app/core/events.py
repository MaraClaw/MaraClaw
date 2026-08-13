"""Redis Pub/Sub events for enterprise info sync."""

import json

import redis.asyncio as redis

from app.config import get_settings
from app.core.json_types import JsonObject

settings = get_settings()

_redis_client: redis.Redis | None = None


async def get_redis() -> redis.Redis:
    """Get or create the Redis client."""
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis_client


async def publish_event(channel: str, data: JsonObject) -> None:
    """Publish an event to a Redis Pub/Sub channel."""
    r = await get_redis()
    await r.publish(channel, json.dumps(data))


async def close_redis() -> None:
    """Close the Redis connection."""
    global _redis_client
    if _redis_client:
        await _redis_client.aclose()
        _redis_client = None
