"""Engine-hosted ChatGPT OAuth redirect target: ``GET`` on the path of ``CHATGPT_OAUTH_REDIRECT_URI``.

Unauthenticated by design: the provider redirect carries no admin session, so the single-use state is the only key and
the owner is the administrator recorded at start. ``build_callback_router`` reads the path when it runs, once at
``app.main`` import, so changing ``CHATGPT_OAUTH_REDIRECT_URI`` needs a restart; empty registers no route. Mounted
without ``API_PREFIX`` and outside the OpenAPI schema. The query (state, code, provider prose) is never logged or echoed.
"""

import base64
import hashlib
from typing import Final
from urllib.parse import unquote, urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.config import get_settings
from app.services.chatgpt_subscription_handoff import CallbackOutcome, complete_chatgpt_subscription_callback

_STYLE: Final = "body{font:16px/1.5 system-ui,sans-serif;margin:4rem auto;max-width:32rem;padding:0 1rem}"
_STYLE_HASH: Final = base64.b64encode(hashlib.sha256(_STYLE.encode()).digest()).decode()
_HEADERS: Final = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": (
        f"default-src 'none'; style-src 'sha256-{_STYLE_HASH}'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'none'"
    ),
}
_STATUS: Final[dict[CallbackOutcome, int]] = {"authorized": 200, "rejected": 400, "unavailable": 503}
_FINISHED: Final = ("ChatGPT sign-in finished", "You can close this tab and return to MaraClaw.")
# One wording for every failure: the page must not tell whether a state was ever valid.
_FAILED: Final = ("ChatGPT sign-in could not be completed", "Close this tab and connect ChatGPT again from MaraClaw.")


def _page(outcome: CallbackOutcome) -> HTMLResponse:
    title, message = _FINISHED if outcome == "authorized" else _FAILED
    body = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{title}</title><style>{_STYLE}</style></head>"
        f"<body><main><h1>{title}</h1><p>{message}</p></main></body></html>"
    )
    return HTMLResponse(body, status_code=_STATUS[outcome], headers=_HEADERS)


async def chatgpt_oauth_callback(request: Request) -> HTMLResponse:
    """Settle the sign-in the provider redirected here; the admin console reads the result through status."""
    # The raw bytes, not request.url: a non-UTF-8 query must be rejected by the parser, not fail decoding here.
    raw: object = request.scope.get("query_string")
    query = raw.decode("latin-1") if isinstance(raw, bytes) else ""
    return _page(await complete_chatgpt_subscription_callback(query))


def build_callback_router() -> APIRouter:
    """Router with the callback at the configured redirect path, or no route when ChatGPT sign-in is off."""
    router = APIRouter(tags=["chatgpt-oauth"])
    redirect_uri = get_settings().CHATGPT_OAUTH_REDIRECT_URI
    if redirect_uri:
        # Starlette matches the decoded request path.
        router.add_api_route(
            unquote(urlsplit(redirect_uri).path),
            chatgpt_oauth_callback,
            methods=["GET"],
            include_in_schema=False,
            response_class=HTMLResponse,
        )
    return router
