"""Guest image rule shared by guest starts and OpenClaw upgrades.

A reference with a registry host, a namespace or a digest may be pulled. A bare, non-digest name
(``arm64-openclaw:2026.11.3``) or an image id is local to the worker's Docker daemon and is never pulled.
"""

from anyio import to_thread
from pywhales import ClientNotFoundError, DockerClient
from pywhales.client_config import ParsingError
from pywhales.exceptions import DockerException

from app.config import get_settings
from app.core.logging import logger
from app.services.guest_admission import GuestStartPreflightError


def image_repository(reference: str) -> str:
    """The reference without its tag (a colon followed by a slash belongs to a registry port)."""
    head, separator, tail = reference.rpartition(":")
    return head if separator and "/" not in tail else reference


def is_local_reference(reference: str) -> bool:
    return "@" not in reference and "/" not in image_repository(reference)


def _settle_image(client: DockerClient, image: str) -> None:
    if client.image.exists(image):
        return
    if image.startswith("sha256:"):
        raise GuestStartPreflightError(f"Guest image {image} is missing on this Docker daemon", code="runtime_failed")
    if is_local_reference(image):
        raise GuestStartPreflightError(
            f"Local guest image {image} is missing; build it with the guest image project's "
            "build-openclaw-local-dockerfile.sh on this Docker daemon before starting the guest",
            code="runtime_failed",
        )
    _ = client.image.pull(image, quiet=True, timeout=get_settings().OPENCLAW_GUEST_IMAGE_PULL_TIMEOUT_SECONDS)


async def ensure_guest_image(client: DockerClient, image: str) -> None:
    """Make ``image`` present locally before guest admission or any destructive step.

    A missing registry or digest reference is pulled once under the configured deadline; a missing local
    reference or image id is refused without a pull. Docker failures, deadline expiry and malformed Docker
    output included, become a preflight error whose reason is logged here, since callers report only its code.
    """
    try:
        await to_thread.run_sync(_settle_image, client, image)
    except (GuestStartPreflightError, ClientNotFoundError, DockerException, ParsingError) as exc:
        logger.warning("Guest image {} is unavailable: {}", image, exc)
        raise GuestStartPreflightError("Guest image is unavailable", code="runtime_failed") from exc
