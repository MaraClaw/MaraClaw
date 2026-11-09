from app.core.outbound_transport import public_http_client


async def download_image(url: str) -> bytes:
    """Fetch an untrusted image reference without generation credentials or cookies."""
    async with public_http_client(timeout=60, follow_redirects=True) as client:
        response = await client.get(url)
        _ = response.raise_for_status()
        return response.content
