"""Blocking Docker operations used by the OpenClaw upgrade executor.

Every method blocks; callers run them through ``anyio.to_thread.run_sync``.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass

from python_on_whales import DockerClient
from python_on_whales.exceptions import NoSuchContainer, NoSuchImage

from app.services.guest_identity import AGENT_LABEL
from app.services.guest_probe import probe_guest


@dataclass(frozen=True, slots=True)
class ContainerInfo:
    id: str
    name: str
    image_id: str
    agent_id: str | None


_DIGEST_LINE = re.compile(r"^Digest: (sha256:[0-9a-f]{64})$")


def _repository(reference: str) -> str:
    """The reference without its tag (a colon followed by a slash belongs to a registry port)."""
    head, separator, tail = reference.rpartition(":")
    return head if separator and "/" not in tail else reference


class UpgradeDocker:
    def __init__(self, client: DockerClient) -> None:
        self.client: DockerClient = client

    def pull(self, reference: str) -> str:
        """Resolve local images or freshly pull registry images; return an immutable image id.

        The SDK's returned ``Image`` is a lazy handle on the tag, so inspecting it (or the tag)
        after the pull can see a concurrently retargeted tag. The ``Digest:`` line the daemon
        prints names the pulled manifest; ``repo@digest`` cannot be retargeted. A digest-pinned
        configuration is pulled too (it may not be cached locally) and then inspected as is.
        """
        if "@" not in reference and "/" not in _repository(reference):
            try:
                return self.client.image.inspect(reference).id
            except NoSuchImage as exc:
                raise OSError(
                    f"Local OpenClaw image {reference} is missing; build it with the Photon project's "
                    "build-openclaw-local-dockerfile.sh "
                    "on the worker's Docker daemon before retrying the upgrade"
                ) from exc
        stream = self.client.image.pull(reference, stream_logs=True)
        if not isinstance(stream, Iterator):
            raise OSError(f"docker pull of {reference} did not stream its log")
        digest: str | None = None
        for _source, line in stream:
            matched = _DIGEST_LINE.match(line.decode(errors="replace").strip())
            if matched is not None:
                digest = str(matched.group(1))
        if "@" in reference:
            return self.client.image.inspect(reference).id
        if digest is None:
            raise OSError(f"docker pull of {reference} reported no digest")
        return self.client.image.inspect(f"{_repository(reference)}@{digest}").id

    def inspect(self, reference: str) -> ContainerInfo | None:
        try:
            container = self.client.container.inspect(reference)
        except NoSuchContainer:
            return None
        return ContainerInfo(
            id=container.id,
            name=container.name,
            image_id=container.image,
            agent_id=(container.config.labels or {}).get(AGENT_LABEL),
        )

    def image_exists(self, image_id: str) -> bool:
        try:
            _ = self.client.image.inspect(image_id)
        except NoSuchImage:
            return False
        return True

    def stop(self, reference: str) -> None:
        self.client.container.stop(reference, time=10)

    def start(self, reference: str) -> None:
        _ = self.client.container.start(reference)

    def set_restart(self, reference: str, policy: str) -> None:
        self.client.container.update(reference, restart=policy)

    def rename(self, reference: str, name: str) -> None:
        self.client.container.rename(reference, name)

    def remove(self, reference: str) -> None:
        """Force-remove; an already-missing container is the desired end state."""
        with suppress(NoSuchContainer):
            # ty misbinds ContainerCLI.remove through DockerClient.container; call the descriptor explicitly.
            type(self.client.container).remove(self.client.container, [reference], force=True)

    def ready(self, reference: str, timeout: float) -> bool:
        """One in-guest ``/readyz`` probe; Docker liveness and engine-side HTTP prove nothing here."""
        return probe_guest(self.client, reference, timeout)
