import json

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_media_download_hosts_default_to_no_trusted_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZALO_PERSONAL_MEDIA_ALLOWED_HOSTS", raising=False)
    assert Settings().ZALO_PERSONAL_MEDIA_ALLOWED_HOSTS == []


def test_media_download_hosts_accept_an_explicit_host_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZALO_PERSONAL_MEDIA_ALLOWED_HOSTS", '["media.example.com","files.example.com"]')
    assert Settings().ZALO_PERSONAL_MEDIA_ALLOWED_HOSTS == ["media.example.com", "files.example.com"]


@pytest.mark.parametrize(
    "hosts",
    [
        ["https://media.example.com/path"],
        ["*.example.com"],
        ["127.0.0.1"],
        ["[::1]"],
        ["localhost"],
        ["media.example.com:443"],
        ["user@media.example.com"],
        ["-media.example.com"],
        ["media..example.com"],
        ["media.example.com."],
        ["MEDIA.example.com"],
        ["media.example.a"],
        ["media.example.xn--p1ai"],
        ["a" * 64 + ".example.com"],
        ["media.example.com"] * 33,
    ],
)
def test_media_download_hosts_reject_non_exact_or_unbounded_hosts(
    monkeypatch: pytest.MonkeyPatch, hosts: list[str]
) -> None:
    monkeypatch.setenv("ZALO_PERSONAL_MEDIA_ALLOWED_HOSTS", json.dumps(hosts))
    with pytest.raises(ValidationError):
        Settings()
