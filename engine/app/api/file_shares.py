"""Public download route for file share links: no login, the unguessable token in the URL is the credential."""

from fastapi import APIRouter, Request
from starlette.responses import Response

from app.services import file_share_links

# No /api prefix and no request-wide database binding: the token store is Redis and the file is streamed.
public_router = APIRouter(tags=["file-shares"])


@public_router.api_route("/f/{token}/{filename}", methods=["GET", "HEAD"], response_model=None)
async def download_shared_file(token: str, filename: str, request: Request) -> Response:
    """Serve the file a share token points at; `filename` must be the shared file's own name."""
    return await file_share_links.open_share(token, filename, head=request.method == "HEAD")
