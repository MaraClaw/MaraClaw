"""DingTalk access_token global cache manager.

Caches tokens per app_key with auto-refresh before expiry.
All DingTalk token acquisition should go through this manager.
"""

import time
from json import JSONDecodeError

import httpx

from app.core.json_types import json_as_int, json_as_str_or, json_object_from_response
from app.core.logging import logger
from app.services.im_token_cache import ImTokenKey, MintedToken, get_or_mint_im_token


class DingTalkTokenManager:
    """Global DingTalk access_token cache.

    - Cache by app_key
    - Token valid for 7200s, refresh 300s early
    - One shared mint per key (``get_or_mint_im_token``)
    """

    def __init__(self):
        self._cache: dict[str, tuple[str, float]] = {}

    async def get_token(self, app_key: str, app_secret: str) -> str | None:
        """Get access_token, return cached if valid, refresh if expired."""
        local_key = f"{app_key}\0{app_secret}"
        if local_key in self._cache:
            token, expires_at = self._cache[local_key]
            if time.time() < expires_at - 300:
                return token

        async def mint() -> MintedToken:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    "https://api.dingtalk.com/v1.0/oauth2/accessToken",
                    json={"appKey": app_key, "appSecret": app_secret},
                )
            _ = resp.raise_for_status()
            data = json_object_from_response(resp)
            token = json_as_str_or(data.get("accessToken"), "")
            expires_in = json_as_int(data.get("expireIn"), 7200)
            if not token:
                logger.error(f"[DingTalk Token] Failed to get token: {data}")
                return MintedToken("")
            self._cache[local_key] = (token, time.time() + expires_in)
            logger.debug(f"[DingTalk Token] Refreshed for {app_key[:8]}..., expires in {expires_in}s")
            return MintedToken(token, expires_in)

        try:
            return await get_or_mint_im_token(ImTokenKey("dingtalk", app_key, app_secret), mint) or None
        except httpx.HTTPStatusError as e:
            if e.response.is_server_error:
                raise
            logger.error(f"[DingTalk Token] Error getting token: {e}")
            return None
        except JSONDecodeError as e:
            logger.error(f"[DingTalk Token] Invalid token response: {e}")
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
