"""Immutable image capture on pull: the id comes from the pulled digest, never from the mutable tag."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import override

import pytest
from python_on_whales import DockerClient, Image

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
        return iter([(reference, line.encode()) for line in daemon.pull_lines])

    def inspect(reference: str) -> Image:
        if not cached and not daemon.pulled:
            raise OSError("No such image")
        daemon.inspected.append(reference)
        return _KnownImage(client, PULLED_ID if "@" in reference else RETARGETED_ID)

    monkeypatch.setattr(client.image, "pull", pull)
    monkeypatch.setattr(client.image, "inspect", inspect)
    return UpgradeDocker(client), daemon


def test_pull_returns_the_pulled_digests_image_even_if_the_tag_moved(monkeypatch: pytest.MonkeyPatch):
    docker, daemon = _docker(
        monkeypatch, "1.2.3: Pulling from ns/openclaw", f"Digest: {DIGEST}", "Status: Downloaded newer image"
    )
    assert docker.pull("registry.example:5000/ns/openclaw:1.2.3") == PULLED_ID
    assert daemon.inspected == [f"registry.example:5000/ns/openclaw@{DIGEST}"]


def test_pull_without_a_reported_digest_fails_instead_of_trusting_the_tag(monkeypatch: pytest.MonkeyPatch):
    docker, daemon = _docker(monkeypatch, "Status: Image is up to date")
    with pytest.raises(OSError, match="no digest"):
        _ = docker.pull("openclaw:1")
    assert daemon.inspected == []


def test_digest_pinned_reference_is_pulled_before_it_is_inspected(monkeypatch: pytest.MonkeyPatch):
    # Given a digest-pinned image that is not cached locally: inspect only succeeds after a pull.
    docker, daemon = _docker(monkeypatch, f"Digest: {DIGEST}", cached=False)
    assert docker.pull(f"ns/openclaw@{DIGEST}") == PULLED_ID
    assert daemon.pulled == [f"ns/openclaw@{DIGEST}"]
    assert daemon.inspected == [f"ns/openclaw@{DIGEST}"]


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
