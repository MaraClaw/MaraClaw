"""Detect duplicates and static handlers hidden by first-FULL dispatch."""

import re

from fastapi import APIRouter, FastAPI
from starlette.routing import Match

from app.main import app
from route_dispatch import EffectiveRoute, effective_routes


def path_shape(path: str) -> str:
    """Drop parameter names but keep convertors: /a/{x} == /a/{y:str} != /a/{z:path}."""
    return re.sub(r"\{[^{}:]+(?::([^{}]+))?\}", lambda m: "{" + (m[1] or "str") + "}", path)


def find_route_conflicts(routes: list[EffectiveRoute]) -> list[str]:
    conflicts = []
    for position, route in enumerate(routes):
        shape = path_shape(route.path)
        for method in sorted(route.methods):
            duplicates = []
            for earlier in routes[:position]:
                if (
                    earlier.protocol == route.protocol
                    and method in earlier.methods
                    and path_shape(earlier.path) == shape
                ):
                    conflicts.append(
                        f"DUPLICATE {method} {route.path} ({route.handler}); earlier {earlier.path} ({earlier.handler})"
                    )
                    duplicates.append(earlier.index)
            if "{" not in route.path:
                for earlier in routes[:position]:
                    if earlier.match(route.protocol, method, route.path) == Match.FULL:
                        if earlier.index not in duplicates:
                            conflicts.append(
                                f"SHADOW {method} {route.path} ({route.handler}); "
                                f"earlier {earlier.path} ({earlier.handler})"
                            )
                        break
    return conflicts


def test_mounted_routes_are_reachable() -> None:
    conflicts = find_route_conflicts(effective_routes(app))
    assert conflicts == [], (
        "Unreachable routes (Starlette dispatches to the first full match in registration order):\n"
        + "\n".join(conflicts)
    )


def test_included_router_conflicts_respect_order_and_methods() -> None:
    router = APIRouter()

    async def first() -> None:
        pass

    async def second() -> None:
        pass

    for path, methods, endpoint in [
        ("/tools/bulk", ["PUT"], first),
        ("/tools/{tool_id}", ["PUT"], first),
        ("/tools/mcp-server", ["PUT"], second),
        ("/tools/read", ["GET"], second),
        ("/same", ["POST"], first),
        ("/same", ["POST"], second),
        ("/params/{x}", ["GET"], first),
        ("/params/{y:str}", ["GET"], second),
        ("/params/{z:path}", ["GET"], second),
    ]:
        router.add_api_route(path, endpoint, methods=methods)
    nested = APIRouter()
    nested.include_router(router, prefix="/v1")
    synthetic = FastAPI()
    synthetic.include_router(nested, prefix="/api")
    name = f"{first.__module__}.{first.__qualname__.rsplit('.', 1)[0]}"
    assert find_route_conflicts(effective_routes(synthetic)) == [
        f"SHADOW PUT /api/v1/tools/mcp-server ({name}.second); earlier /api/v1/tools/{{tool_id}} ({name}.first)",
        f"DUPLICATE POST /api/v1/same ({name}.second); earlier /api/v1/same ({name}.first)",
        f"DUPLICATE GET /api/v1/params/{{y:str}} ({name}.second); earlier /api/v1/params/{{x}} ({name}.first)",
    ]
