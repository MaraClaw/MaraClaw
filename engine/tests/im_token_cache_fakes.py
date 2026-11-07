import pytest

from app.services import im_token_cache


def install_token_cache(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str, str], str]:
    """In-memory stand-in for the Redis IM token cache, keyed (provider, subject, secret)."""
    store: dict[tuple[str, str, str], str] = {}

    async def get(provider: str, subject: str, *, secret: str = "") -> str | None:
        return store.get((provider, subject, secret))

    async def put(provider: str, subject: str, token: str, *, ttl: int, secret: str = "") -> None:
        store[(provider, subject, secret)] = token

    async def drop(provider: str, subject: str, *, secret: str = "") -> None:
        _ = store.pop((provider, subject, secret), None)

    async def drop_rejected(key: im_token_cache.ImTokenKey, rejected: str) -> None:
        if store.get((key.provider, key.subject, key.secret)) == rejected:
            _ = store.pop((key.provider, key.subject, key.secret))

    monkeypatch.setattr(im_token_cache, "get_cached_im_token", get)
    monkeypatch.setattr(im_token_cache, "set_cached_im_token", put)
    monkeypatch.setattr(im_token_cache, "drop_cached_im_token", drop)
    monkeypatch.setattr(im_token_cache, "drop_rejected_im_token", drop_rejected)
    return store
