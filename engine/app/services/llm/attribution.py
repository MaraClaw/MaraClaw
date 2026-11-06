"""Provider app-attribution headers that identify MaraClaw (``settings.APP_NAME``) instead of OpenClaw.

Only OpenRouter documents a configurable app title; attribution is selected by endpoint host, like OpenClaw does, so
a custom provider pointed at OpenRouter is covered and a proxy host is not.
"""

from urllib.parse import urlsplit

OPENROUTER_TITLE_HEADER = "X-OpenRouter-Title"
_OPENROUTER_HOST = "openrouter.ai"


def openrouter_attribution_headers(base_url: str | None, app_name: str) -> dict[str, str]:
    """Return the OpenRouter title header for an ``openrouter.ai`` endpoint, otherwise no headers."""
    title = app_name.strip()
    if not title or not (title.isascii() and title.isprintable()) or base_url is None:
        return {}
    host = (urlsplit(base_url.strip()).hostname or "").rstrip(".")
    if host != _OPENROUTER_HOST and not host.endswith(f".{_OPENROUTER_HOST}"):
        return {}
    return {OPENROUTER_TITLE_HEADER: title}
