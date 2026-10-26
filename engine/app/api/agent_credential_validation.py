from collections.abc import Callable, Coroutine
from typing import override

from fastapi import Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from app.core.json_types import object_mapping_from


class CredentialSafeRoute(APIRoute):
    @override
    def get_route_handler(self) -> Callable[[Request], Coroutine[object, object, Response]]:
        handler = super().get_route_handler()

        async def safe_handler(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError as error:
                issues = [object_mapping_from(item) for item in list[object](error.errors())]
                return JSONResponse(
                    status_code=422,
                    content={"detail": [{key: item[key] for key in ("type", "loc", "msg")} for item in issues]},
                )

        return safe_handler
