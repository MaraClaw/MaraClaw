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


def _isolated_headers(style: str) -> dict[str, str]:
    style_hash = base64.b64encode(hashlib.sha256(style.encode()).digest()).decode()
    return {
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": (
            f"default-src 'none'; style-src 'sha256-{style_hash}'; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'"
        ),
    }


def _document(title: str, style: str, main: str) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{title}</title><style>{style}</style></head><body><main>{main}</main></body></html>"
    )


# One wording for every failure: the page must not tell whether a state was ever valid.
_FAILED_TITLE: Final = "ChatGPT sign-in could not be completed"
_FAILED_STYLE: Final = "body{font:16px/1.5 system-ui,sans-serif;margin:4rem auto;max-width:32rem;padding:0 1rem}"
_FAILED_PAGE: Final = _document(
    _FAILED_TITLE,
    _FAILED_STYLE,
    f"<h1>{_FAILED_TITLE}</h1><p>Close this tab and connect ChatGPT again from MaraClaw.</p>",
)
_FAILED_HEADERS: Final = _isolated_headers(_FAILED_STYLE)

# Colors, shadow and selection mirror the web-a tokens (web-a/src/index.css :root and .dark); the stylesheet stays
# on one line because the CSP hash covers its exact bytes.
_CONNECTED_TITLE: Final = "ChatGPT subscription connected"
_CONNECTED_STYLE: Final = (
    ":root{color-scheme:light dark;--bg:oklch(.985 .004 80);--fg:oklch(.2 .02 45);--card:oklch(1 0 0);"
    "--muted:oklch(.42 .02 50);--line:oklch(.88 .012 75);--brand:oklch(.52 .16 38);"
    "--shadow:0 1px 0 oklch(1 0 0/.8) inset,0 1px 2px oklch(.35 .02 45/.04),0 12px 32px -16px oklch(.35 .02 45/.14)}"
    "@media (prefers-color-scheme:dark){:root{--bg:oklch(.155 .014 40);--fg:oklch(.97 .008 80);"
    "--card:oklch(.2 .016 40);--muted:oklch(.78 .018 65);--line:oklch(1 0 0/.12);--brand:oklch(.74 .15 42);"
    "--shadow:0 1px 0 oklch(1 0 0/.05) inset,0 18px 40px -28px oklch(0 0 0/.55)}}"
    "body{box-sizing:border-box;min-height:100svh;margin:0;padding:1rem;display:grid;"
    "grid-template-columns:minmax(0,1fr);place-items:center;overflow-wrap:anywhere;"
    "background:var(--bg);color:var(--fg);font:1rem/1.5 Inter,ui-sans-serif,system-ui,sans-serif;"
    "-webkit-font-smoothing:antialiased}"
    "main{box-sizing:border-box;width:100%;max-width:30rem;padding:clamp(1.5rem,6vw,2.5rem);"
    "background:var(--card);border:1px solid var(--line);border-radius:1rem;box-shadow:var(--shadow)}"
    '.brand,h1{font-family:"Instrument Sans",Inter,ui-sans-serif,system-ui,sans-serif;font-weight:600}'
    ".brand{display:flex;align-items:center;gap:.5rem;margin:0 0 2rem;font-size:1.125rem}"
    ".brand svg{flex:none;width:2rem;height:2rem;color:var(--brand)}"
    ".check{display:block;width:3rem;height:3rem;margin:0 0 1rem;color:var(--brand)}"
    "h1{margin:0 0 .5rem;font-size:1.5rem;line-height:1.25;letter-spacing:-.02em;text-wrap:balance}"
    "p{margin:0;color:var(--muted);text-wrap:pretty}"
    "::selection{background:oklch(.72 .16 40/.3);color:var(--fg)}"
)
# Monochrome mark: shapes copied from web-e/public/maraclaw-mark.svg; the wordmark beside it is text.
_MARK: Final = (
    '<svg viewBox="0 0 64 64" aria-hidden="true"><g fill="currentColor"><circle cx="28.5" cy="36.5" r="7.2"/>'
    '<path d="M31.2 30.6C36.2 24.8 42.4 20.2 49.2 18.4C50.8 18 52 19.6 51.2 21.1C48.4 26.2 44.1 30.4 38.8 33.4'
    'C36.6 34.7 33.8 34.1 31.8 32.4C31.2 31.9 31 31.2 31.2 30.6Z"/>'
    '<path d="M33.4 39.2C39.8 40.6 45.6 44.2 49.8 49.6C50.8 50.9 49.6 52.6 48 52C41.8 49.6 36.4 45.4 32.4 40.2'
    'C31.4 38.9 32.1 38.8 33.4 39.2Z"/>'
    '<path d="M22.6 33.4C17.2 29.8 12.8 24.6 10.4 18.6C9.8 17.1 11.4 15.8 12.8 16.6C18.2 19.6 22.6 24.2 25.4 29.6'
    'C26.4 31.5 25 33.4 22.6 33.4Z"/><circle cx="24.8" cy="41.8" r="3.4"/></g>'
    '<g transform="translate(40.5 31.5)"><circle r="4.4" fill="currentColor" opacity="0.22"/>'
    '<path d="M0 -2.6 L0.7 -0.7 L2.6 0 L0.7 0.7 L0 2.6 L-0.7 0.7 L-2.6 0 L-0.7 -0.7 Z" fill="currentColor"/></g></svg>'
)
_CHECK: Final = (
    '<svg class="check" viewBox="0 0 48 48" aria-hidden="true"><circle cx="24" cy="24" r="24" fill="currentColor" '
    'opacity=".14"/><path d="M15 24.5l6 6 12-13" fill="none" stroke="currentColor" stroke-width="3" '
    'stroke-linecap="round" stroke-linejoin="round"/></svg>'
)
_CONNECTED_PAGE: Final = _document(
    _CONNECTED_TITLE,
    _CONNECTED_STYLE,
    f'<div class="brand">{_MARK}<span>MaraClaw</span></div>{_CHECK}<h1>{_CONNECTED_TITLE}</h1>'
    "<p>You can close this tab and return to Models in MaraClaw.</p>",
)
_CONNECTED_HEADERS: Final = _isolated_headers(_CONNECTED_STYLE)


def _page(outcome: CallbackOutcome) -> HTMLResponse:
    match outcome:
        case "authorized":
            return HTMLResponse(_CONNECTED_PAGE, headers=_CONNECTED_HEADERS)
        case "rejected":
            return HTMLResponse(_FAILED_PAGE, status_code=400, headers=_FAILED_HEADERS)
        case "unavailable":
            return HTMLResponse(_FAILED_PAGE, status_code=503, headers=_FAILED_HEADERS)


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
