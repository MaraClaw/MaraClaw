"""Dispatch-order probes shared by the route and client regression guards."""

from dataclasses import dataclass
from typing import Literal

from fastapi import FastAPI
from fastapi.routing import RouteContext, iter_route_contexts
from starlette.routing import Match, WebSocketRoute

type Protocol = Literal["http", "websocket"]


@dataclass(frozen=True, slots=True)
class EffectiveRoute:
    index: int
    path: str
    protocol: Protocol
    methods: frozenset[str]
    handler: str
    context: RouteContext

    def match(self, protocol: Protocol, method: str, path: str) -> Match:
        # Each match gets a fresh scope: RouteContext.matches mutates it.
        scope = {"type": protocol, "path": path, "root_path": "", "headers": [], "query_string": b""}
        if protocol == "http":
            scope["method"] = method
        matched = self.context.matches(scope)[0]
        assert isinstance(matched, Match)
        return matched


@dataclass(frozen=True, slots=True)
class Resolution:
    outcome: Literal["full", "partial", "none"]
    route: EffectiveRoute | None
    path: str
    redirected: bool = False


def effective_routes(app: FastAPI) -> list[EffectiveRoute]:
    """Flatten included routers using FastAPI's public, prefix-aware iterator."""
    routes = []
    # Low-priority app.frontend() routes cannot shadow normal routes; this app has none.
    for index, ctx in enumerate(iter_route_contexts(app.routes)):
        owner = next(cls for cls in type(ctx.original_route).__mro__ if "matches" in cls.__dict__)
        assert owner.__module__.startswith(("fastapi.", "starlette.")), (
            f"Unsupported custom matches override: {owner.__module__}.{owner.__qualname__} at {ctx.path}; "
            "RouteContext.matches would bypass it"
        )
        # Included websocket contexts expose empty metadata in 0.143; use their prefixed Starlette route.
        if isinstance(ctx.original_route, WebSocketRoute):
            websocket = getattr(ctx, "starlette_route", None)
            if websocket is not None:
                assert isinstance(websocket, WebSocketRoute), "Guard requires a concrete websocket route"
                ctx = RouteContext(websocket)
        endpoint = ctx.endpoint
        assert ctx.path is not None, "Guard requires a concrete route path"
        assert endpoint is not None, "Guard requires a concrete endpoint"
        protocol: Protocol = "http" if ctx.methods is not None else "websocket"
        routes.append(
            EffectiveRoute(
                index,
                ctx.path,
                protocol,
                frozenset(ctx.methods) if ctx.methods is not None else frozenset({"WEBSOCKET"}),
                f"{endpoint.__module__}.{getattr(endpoint, '__qualname__', repr(endpoint))}",
                ctx,
            )
        )
    return routes


def resolve(
    routes: list[EffectiveRoute], protocol: Protocol, method: str, path: str, *, redirect_slashes: bool
) -> Resolution:
    """Find the first FULL, then first PARTIAL, then a successful HTTP slash redirect."""
    partial = None
    for route in routes:
        matched = route.match(protocol, method, path)
        if matched == Match.FULL:
            return Resolution("full", route, path)
        if matched == Match.PARTIAL and partial is None:
            partial = route
    if partial is not None:
        return Resolution("partial", partial, path)
    if protocol == "http" and redirect_slashes and path != "/":
        toggled = path.rstrip("/") if path.endswith("/") else path + "/"
        redirected = resolve(routes, protocol, method, toggled, redirect_slashes=False)
        if redirected.outcome == "full":
            return Resolution("full", redirected.route, toggled, True)
    return Resolution("none", None, path)
