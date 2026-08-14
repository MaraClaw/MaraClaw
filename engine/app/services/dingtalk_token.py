"""DingTalk access_token global cache manager.

Caches tokens per app_key with auto-refresh before expiry.
All DingTalk token acquisition should go through this manager.
"""

import asyncio
import time

import httpx

from app.core.logging import logger


class DingTalkTokenManager:
    """Global DingTalk access_token cache.

    - Cache by app_key
    - Token valid for 7200s, refresh 300s early
    - Concurrency-safe with asyncio.Lock
    """

    def __init__(self):
        self._cache: dict[str, tuple[str, float]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _get_lock(self, app_key: str) -> asyncio.Lock:
        if app_key not in self._locks:
            self._locks[app_key] = asyncio.Lock()
        return self._locks[app_key]

    async def get_token(self, app_key: str, app_secret: str) -> str | None:
        """Get access_token, return cached if valid, refresh if expired."""
        from app.services.im_token_cache import get_cached_im_token, refresh_ttl, set_cached_im_token

        local_key = f"{app_key}\0{app_secret}"
        if local_key in self._cache:
            token, expires_at = self._cache[local_key]
            if time.time() < expires_at - 300:
                return token
        shared = await get_cached_im_token("dingtalk", app_key, secret=app_secret)
        if shared:
            return shared

        async with self._get_lock(local_key):
            # Double-check after acquiring lock
            if local_key in self._cache:
                token, expires_at = self._cache[local_key]
                if time.time() < expires_at - 300:
                    return token
            shared = await get_cached_im_token("dingtalk", app_key, secret=app_secret)
            if shared:
                return shared

            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.post(
                        "https://api.dingtalk.com/v1.0/oauth2/accessToken",
                        json={"appKey": app_key, "appSecret": app_secret},
                    )
                    data = resp.json()
                    token = data.get("accessToken")
                    expires_in = data.get("expireIn", 7200)

                    if token:
                        self._cache[local_key] = (token, time.time() + expires_in)
                        await set_cached_im_token(
                            "dingtalk",
                            app_key,
                            token,
                            secret=app_secret,
                            ttl=refresh_ttl(expires_in),
                        )
                        logger.debug(f"[DingTalk Token] Refreshed for {app_key[:8]}..., expires in {expires_in}s")
                        return token

                    logger.error(f"[DingTalk Token] Failed to get token: {data}")
                    return None
            except Exception as e:
                logger.error(f"[DingTalk Token] Error getting token: {e}")
                return None

    async def get_corp_token(self, app_key: str, app_secret: str) -> str | None:
        """Get corp access_token via oapi.dingtalk.com/gettoken (GET).

        Used for corp API calls like /topapi/v2/user/get.
        Shares the same cache since the token works for both APIs.
        """
        # The v1.0 OAuth2 token also works for corp APIs, so reuse it
        return await self.get_token(app_key, app_secret)


# Global singleton
dingtalk_token_manager = DingTalkTokenManager()
