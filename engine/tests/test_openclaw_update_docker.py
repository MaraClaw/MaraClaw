"""Immutable upgrade image capture from local images or freshly pulled registry manifests."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import override

import pytest
from pywhales import DockerClient, Image
from pywhales.exceptions import DockerException, NoSuchImage

from app.services.openclaw_update_docker import UpgradeDocker, _repository

DIGEST = "sha256:" + "a" * 64
PULLED_ID = "sha256:" + "1" * 64
RETARGETED_ID = "sha256:" + "2" * 64


class _KnownImage(Image):
    """An SDK image handle whose id is fixed instead of lazily inspected from the daemon."""

    def __init__(self, client: DockerClient, image_id: str) -> None:
        super().__init__(client.client_config, image_id, is_immutable_id=True)
        self._known_id: str = image_id

    @property
    @override
    def id(self) -> str:
        return self._known_id


@dataclass(slots=True)
class _Daemon:
    """What the real SDK client was asked to do; ``pull_lines`` is the daemon's pull log."""

    pull_lines: list[str]
    pulled: list[str] = field(default_factory=list[str])
    inspected: list[str] = field(default_factory=list[str])


def _docker(monkeypatch: pytest.MonkeyPatch, *pull_lines: str, cached: bool = True) -> tuple[UpgradeDocker, _Daemon]:
    """A real ``DockerClient`` whose pull and inspect are replaced by a daemon that retargeted the tag.

    Only a ``repo@digest`` reference still names the pulled image; the bare tag resolves elsewhere.
    ``cached=False`` models an image that does not exist locally until it has been pulled.
    """
    client = DockerClient()
    daemon = _Daemon(list(pull_lines))

    def pull(reference: str, *, stream_logs: bool) -> Iterator[tuple[str, bytes]]:
        assert stream_logs
        daemon.pulled.append(reference)
        yield from ((reference, line.encode()) for line in daemon.pull_lines)

    def inspect(reference: str) -> Image:
        daemon.inspected.append(reference)
        if not cached and not daemon.pulled:
            raise NoSuchImage(["docker", "image", "inspect", reference], 1, stderr=b"No such image")
        return _KnownImage(client, PULLED_ID if "@" in reference else RETARGETED_ID)

    monkeypatch.setattr(client.image, "pull", pull)
    monkeypatch.setattr(client.image, "inspect", inspect)
    return UpgradeDocker(client), daemon


@pytest.mark.parametrize("reference", ["openclaw:2026.10.8", "openclaw"])
def test_local_image_returns_immutable_id_without_pulling(monkeypatch: pytest.MonkeyPatch, reference: str) -> None:
    # Given a cached local image distinct from the registry's image.
    docker, daemon = _docker(monkeypatch, f"Digest: {DIGEST}")
    # When an upgrade resolves the local reference.
    image_id = docker.pull(reference)
    # Then one local inspection supplies the immutable id without a pull.
    assert image_id == RETARGETED_ID
    assert daemon.inspected == [reference]
    assert daemon.pulled == []


@pytest.mark.parametrize("reference", ["openclaw:2026.10.8", "openclaw"])
def test_missing_local_image_requires_build_without_pulling(monkeypatch: pytest.MonkeyPatch, reference: str) -> None:
    # Given a local target absent from the worker's Docker daemon.
    docker, daemon = _docker(monkeypatch, f"Digest: {DIGEST}", cached=False)
    # When an upgrade resolves the missing reference.
    with pytest.raises(OSError, match=r"build-openclaw-local-dockerfile\.sh") as raised:
        _ = docker.pull(reference)
    # Then the error identifies the target and its cause; no registry pull is attempted.
    assert reference in str(raised.value)
    assert isinstance(raised.value.__cause__, NoSuchImage)
    assert daemon.inspected == [reference]
    assert daemon.pulled == []


@pytest.mark.parametrize("reference", ["ns/openclaw:1", "ns/openclaw", "registry.example:5000/ns/openclaw:1"])
def test_cached_registry_image_is_still_pulled(monkeypatch: pytest.MonkeyPatch, reference: str) -> None:
    # Given a cached namespaced image whose tag differs from the registry manifest.
    docker, daemon = _docker(monkeypatch, f"Digest: {DIGEST}")
    # When an upgrade resolves the registry reference.
    image_id = docker.pull(reference)
    # Then it uses the freshly pulled manifest rather than the cached mutable tag.
    assert image_id == PULLED_ID
    assert daemon.pulled == [reference]
    assert daemon.inspected == [f"{_repository(reference)}@{DIGEST}"]


@pytest.mark.parametrize("reference", ["ns/openclaw:1", "registry.example:5000/ns/openclaw:1"])
def test_registry_pull_failure_does_not_use_cached_image(monkeypatch: pytest.MonkeyPatch, reference: str) -> None:
    # Given a cached registry image and a failing registry pull.
    docker, daemon = _docker(monkeypatch)
    failure = DockerException(["docker", "pull", reference], 1, stderr=b"pull access denied")

    def pull(reference: str, *, stream_logs: bool) -> Iterator[tuple[str, bytes]]:
        assert stream_logs
        daemon.pulled.append(reference)
        raise failure

    monkeypatch.setattr(docker.client.image, "pull", pull)
    # When an upgrade attempts the required fresh pull.
    with pytest.raises(DockerException) as raised:
        _ = docker.pull(reference)
    # Then the registry failure propagates without inspecting a cached fallback.
    assert raised.value is failure
    assert daemon.pulled == [reference]
    assert daemon.inspected == []


def test_pull_returns_the_pulled_digests_image_even_if_the_tag_moved(monkeypatch: pytest.MonkeyPatch):
    docker, daemon = _docker(
        monkeypatch, "1.2.3: Pulling from ns/openclaw", f"Digest: {DIGEST}", "Status: Downloaded newer image"
    )
    assert docker.pull("registry.example:5000/ns/openclaw:1.2.3") == PULLED_ID
    assert daemon.inspected == [f"registry.example:5000/ns/openclaw@{DIGEST}"]


def test_pull_without_a_reported_digest_fails_instead_of_trusting_the_tag(monkeypatch: pytest.MonkeyPatch):
    docker, daemon = _docker(monkeypatch, "Status: Image is up to date")
    with pytest.raises(OSError, match="no digest"):
        _ = docker.pull("ns/openclaw:1")
    assert daemon.inspected == []


@pytest.mark.parametrize("reference", [f"ns/openclaw@{DIGEST}", f"openclaw@{DIGEST}"])
def test_digest_pinned_reference_is_pulled_before_it_is_inspected(
    monkeypatch: pytest.MonkeyPatch, reference: str
) -> None:
    # Given a digest-pinned image that is not cached locally: inspect only succeeds after a pull.
    docker, daemon = _docker(monkeypatch, f"Digest: {DIGEST}", cached=False)
    # When an upgrade resolves either a bare or namespaced digest pin.
    image_id = docker.pull(reference)
    # Then it pulls before inspecting the exact pinned manifest.
    assert image_id == PULLED_ID
    assert daemon.pulled == [reference]
    assert daemon.inspected == [reference]


@pytest.mark.parametrize(
    ("reference", "repository"),
    [
        ("openclaw:2026.10.5", "openclaw"),
        ("ns/openclaw", "ns/openclaw"),
        ("registry.example:5000/ns/openclaw", "registry.example:5000/ns/openclaw"),
        ("registry.example:5000/ns/openclaw:tag", "registry.example:5000/ns/openclaw"),
    ],
)
def test_repository_strips_only_the_tag(reference: str, repository: str):
    assert _repository(reference) == repository


@pytest.mark.parametrize("owner", ["deadbeef-0000-4000-8000-000000000001", None])
def test_inspect_carries_the_full_owner_label(monkeypatch: pytest.MonkeyPatch, owner: str | None) -> None:
    # Given the SDK's inspected container metadata, with or without an owner label.
    client = DockerClient()
    labels = {"maraclaw.agent_id": owner} if owner is not None else None
    container = SimpleNamespace(
        id="guest-id", name="legacy-name", image=PULLED_ID, config=SimpleNamespace(labels=labels)
    )
    monkeypatch.setattr(client.container, "inspect", lambda _ref: container)
    # When the adapter captures rollback metadata.
    info = UpgradeDocker(client).inspect("guest-id")
    # Then the executor receives the complete label rather than inferring ownership from its name.
    assert info is not None
    assert info.agent_id == owner
