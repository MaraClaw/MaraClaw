"""``scripts.s3_lifecycle``: the pure rule logic first, then the script against the loopback S3 server."""

from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError
from scripts.s3_lifecycle import RULE_ID, STATE_SNAPSHOT_RULE_ID, LifecycleRule, covers, main, merge

from app.config import get_settings
from s3_wire_server import S3Server
from scripts import s3_lifecycle

PREFIX = "agents/"
SNAPSHOT_PREFIX = "agents/state-snapshots/"
SNAPSHOT_RULE: LifecycleRule = {
    "ID": STATE_SNAPSHOT_RULE_ID,
    "Status": "Enabled",
    "Filter": {"Prefix": SNAPSHOT_PREFIX},
    "NoncurrentVersionExpiration": {"NoncurrentDays": 1},
    "Expiration": {"ExpiredObjectDeleteMarker": True},
}

COVERING: dict[str, LifecycleRule] = {
    "filter-prefix": {
        "ID": "a",
        "Status": "Enabled",
        "Filter": {"Prefix": "agents/"},
        "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
    },
    "legacy-prefix": {
        "ID": "a",
        "Status": "Enabled",
        "Prefix": "agents/",
        "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
    },
    "legacy-empty-prefix": {
        "ID": "a",
        "Status": "Enabled",
        "Prefix": "",
        "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
    },
    "empty-filter": {
        "ID": "a",
        "Status": "Enabled",
        "Filter": {},
        "NoncurrentVersionExpiration": {"NoncurrentDays": 1},
    },
    "empty-filter-prefix": {
        "ID": "a",
        "Status": "Enabled",
        "Filter": {"Prefix": ""},
        "NoncurrentVersionExpiration": {"NoncurrentDays": 1},
    },
    "and-with-prefix-only": {
        "ID": "a",
        "Status": "Enabled",
        "Filter": {"And": {"Prefix": "agents/"}},
        "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
    },
    "shorter-prefix": {
        "ID": "a",
        "Status": "Enabled",
        "Filter": {"Prefix": "agen"},
        "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
    },
}

NOT_COVERING: dict[str, LifecycleRule] = {
    "disabled": {
        "ID": "a",
        "Status": "Disabled",
        "Filter": {"Prefix": "agents/"},
        "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
    },
    "other-prefix": {
        "ID": "a",
        "Status": "Enabled",
        "Filter": {"Prefix": "other/"},
        "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
    },
    "narrower-prefix": {
        "ID": "a",
        "Status": "Enabled",
        "Filter": {"Prefix": "agents/one/"},
        "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
    },
    "and-with-tags": {
        "ID": "a",
        "Status": "Enabled",
        "Filter": {"And": {"Prefix": "agents/", "Tags": [{"Key": "k", "Value": "v"}]}},
        "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
    },
    "and-with-size-bound": {
        "ID": "a",
        "Status": "Enabled",
        "Filter": {"And": {"Prefix": "agents/", "ObjectSizeGreaterThan": 128}},
        "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
    },
    "tag-filter": {
        "ID": "a",
        "Status": "Enabled",
        "Filter": {"Tag": {"Key": "k", "Value": "v"}},
        "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
    },
    "size-filter": {
        "ID": "a",
        "Status": "Enabled",
        "Filter": {"ObjectSizeLessThan": 1024},
        "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
    },
    "no-noncurrent-expiration": {
        "ID": "a",
        "Status": "Enabled",
        "Filter": {"Prefix": "agents/"},
        "Expiration": {"Days": 30},
    },
}

FOREIGN: list[LifecycleRule] = [
    {"ID": "expire-tmp", "Status": "Enabled", "Filter": {"Prefix": "tmp/"}, "Expiration": {"Days": 1}},
    {
        "ID": "archive",
        "Status": "Disabled",
        "Filter": {"Tag": {"Key": "k", "Value": "v"}},
        "Transitions": [{"Days": 90, "StorageClass": "GLACIER"}],
    },
]


@pytest.mark.parametrize("rule", list(COVERING.values()), ids=list(COVERING))
def test_covers_returns_none_when_a_rule_expires_the_prefix(rule):
    assert covers([rule], PREFIX) is None


@pytest.mark.parametrize("rule", list(NOT_COVERING.values()), ids=list(NOT_COVERING))
def test_covers_gives_a_reason_when_the_rule_does_not_apply_to_the_whole_prefix(rule):
    reason = covers([rule], PREFIX)
    assert isinstance(reason, str)
    assert reason


def test_covers_gives_a_reason_without_rules():
    assert covers([], PREFIX) is not None


def test_covers_needs_only_one_covering_rule():
    assert covers([NOT_COVERING["other-prefix"], NOT_COVERING["disabled"], COVERING["filter-prefix"]], PREFIX) is None


def test_covers_within_days_needs_a_short_expiration_without_newer_versions_kept():
    keeping: LifecycleRule = {
        "ID": "a",
        "Status": "Enabled",
        "Filter": {"Prefix": ""},
        "NoncurrentVersionExpiration": {"NoncurrentDays": 1, "NewerNoncurrentVersions": 3},
    }

    assert covers([COVERING["filter-prefix"]], SNAPSHOT_PREFIX) is None
    assert covers([COVERING["filter-prefix"]], SNAPSHOT_PREFIX, within_days=1) is not None
    assert covers([keeping], SNAPSHOT_PREFIX, within_days=1) is not None
    assert covers([COVERING["empty-filter"]], SNAPSHOT_PREFIX, within_days=1) is None
    assert covers([SNAPSHOT_RULE], SNAPSHOT_PREFIX, within_days=1) is None
    assert covers([SNAPSHOT_RULE], PREFIX) is not None


def test_covers_with_an_empty_prefix_needs_a_whole_bucket_rule():
    assert covers([COVERING["filter-prefix"]], "") is not None
    assert covers([COVERING["empty-filter"]], "") is None
    assert covers([COVERING["legacy-empty-prefix"]], "") is None


def test_merge_keeps_every_other_rule_unchanged():
    merged = merge(FOREIGN, PREFIX, 30)

    assert merged[: len(FOREIGN)] == FOREIGN
    assert [rule.get("ID") for rule in merged] == ["expire-tmp", "archive", RULE_ID, STATE_SNAPSHOT_RULE_ID]
    assert len(FOREIGN) == 2


def test_merge_replaces_the_rule_with_the_same_id_in_place():
    own: LifecycleRule = {
        "ID": RULE_ID,
        "Status": "Disabled",
        "Filter": {"Prefix": "old/"},
        "NoncurrentVersionExpiration": {"NoncurrentDays": 7},
    }

    merged = merge([FOREIGN[0], own, FOREIGN[1]], PREFIX, 30)

    assert [rule.get("ID") for rule in merged] == ["expire-tmp", RULE_ID, "archive", STATE_SNAPSHOT_RULE_ID]
    assert merged[0] == FOREIGN[0]
    assert merged[2] == FOREIGN[1]
    assert merged[1].get("Status") == "Enabled"
    assert merged[1].get("Filter") == {"Prefix": PREFIX}


@pytest.mark.parametrize("days", [1, 30, 365])
def test_merge_writes_the_agent_rule_with_the_requested_days_and_the_one_day_snapshot_rule(days):
    assert merge([], PREFIX, days) == [
        {
            "ID": RULE_ID,
            "Status": "Enabled",
            "Filter": {"Prefix": PREFIX},
            "NoncurrentVersionExpiration": {"NoncurrentDays": days},
            "Expiration": {"ExpiredObjectDeleteMarker": True},
            "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7},
        },
        SNAPSHOT_RULE,
    ]


def test_merge_with_an_empty_prefix_uses_an_empty_filter():
    merged = merge([], "", 30)

    assert merged[0].get("Filter") == {}
    assert merged[1].get("Filter") == {"Prefix": "state-snapshots/"}


@pytest.mark.parametrize("prefix", [PREFIX, ""])
def test_merged_rules_cover_the_prefixes_they_were_written_for(prefix):
    merged = merge(FOREIGN, prefix, 30)

    assert covers(merged, prefix) is None
    assert covers(merged, f"{prefix}state-snapshots/", within_days=1) is None


def point_settings(monkeypatch: pytest.MonkeyPatch, server: S3Server, **overrides: str) -> None:
    fields = {
        "STORAGE_BACKEND": "s3",
        "S3_BUCKET": "bucket",
        "S3_PREFIX": "agents",
        "S3_REGION": "",
        "S3_ENDPOINT_URL": server.endpoint,
        "S3_ACCESS_KEY_ID": "key",
        "S3_SECRET_ACCESS_KEY": "secret",
        **overrides,
    }
    settings = get_settings().model_copy(update=fields)
    monkeypatch.setattr(s3_lifecycle, "get_settings", lambda: settings)


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> Iterator[S3Server]:
    server = S3Server()
    server.start()
    point_settings(monkeypatch, server)
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def provider(wire: S3Server):
    """The operator's own view of the bucket: a plain boto3 client, independent of the script."""
    return boto3.Session().client(
        "s3",
        endpoint_url=wire.endpoint,
        aws_access_key_id="key",
        aws_secret_access_key="secret",
        config=Config(proxies={}, s3={"addressing_style": "path"}),
    )


def stored_rules(provider) -> dict[str, dict[str, Any]]:
    return {rule["ID"]: rule for rule in provider.get_bucket_lifecycle_configuration(Bucket="bucket")["Rules"]}


def test_wire_server_serves_versioning_and_lifecycle(wire, provider):
    assert provider.get_bucket_versioning(Bucket="bucket").get("Status") is None
    provider.put_bucket_versioning(Bucket="bucket", VersioningConfiguration={"Status": "Enabled"})
    assert provider.get_bucket_versioning(Bucket="bucket")["Status"] == "Enabled"
    with pytest.raises(ClientError) as raised:
        provider.get_bucket_lifecycle_configuration(Bucket="bucket")
    assert raised.value.response["Error"]["Code"] == "NoSuchLifecycleConfiguration"


@pytest.mark.parametrize("status", ["Enabled", "Suspended"])
def test_check_then_apply_then_check_keeps_foreign_rules(wire, provider, status):
    wire.state.versioning = status
    foreign = {"ID": "expire-tmp", "Status": "Enabled", "Filter": {"Prefix": "tmp/"}, "Expiration": {"Days": 1}}
    provider.put_bucket_lifecycle_configuration(Bucket="bucket", LifecycleConfiguration={"Rules": [foreign]})

    assert main([]) == 1
    assert main(["--check"]) == 1
    assert main(["--apply"]) == 0
    assert main(["--check"]) == 0

    rules = stored_rules(provider)
    assert set(rules) == {"expire-tmp", RULE_ID, STATE_SNAPSHOT_RULE_ID}
    assert rules["expire-tmp"]["Filter"] == {"Prefix": "tmp/"}
    assert rules["expire-tmp"]["Expiration"] == {"Days": 1}
    assert rules[RULE_ID]["NoncurrentVersionExpiration"]["NoncurrentDays"] == 30
    assert rules[STATE_SNAPSHOT_RULE_ID]["Filter"] == {"Prefix": SNAPSHOT_PREFIX}
    assert rules[STATE_SNAPSHOT_RULE_ID]["NoncurrentVersionExpiration"]["NoncurrentDays"] == 1


def test_check_passes_without_versioning_and_writes_nothing(wire):
    assert main(["--check"]) == 0
    assert wire.state.count("PUT") == 0


def test_check_passes_when_a_foreign_rule_already_covers_the_prefix(wire, provider):
    wire.state.versioning = "Enabled"
    covering = {
        "ID": "mine",
        "Status": "Enabled",
        "Filter": {"Prefix": ""},
        "NoncurrentVersionExpiration": {"NoncurrentDays": 1},
    }
    provider.put_bucket_lifecycle_configuration(Bucket="bucket", LifecycleConfiguration={"Rules": [covering]})

    assert main(["--check"]) == 0
    assert wire.state.count("PUT") == 1


def test_check_fails_when_snapshot_versions_outlive_a_day(wire, provider, capsys):
    wire.state.versioning = "Enabled"
    covering = {
        "ID": "mine",
        "Status": "Enabled",
        "Filter": {"Prefix": ""},
        "NoncurrentVersionExpiration": {"NoncurrentDays": 3},
    }
    provider.put_bucket_lifecycle_configuration(Bucket="bucket", LifecycleConfiguration={"Rules": [covering]})

    assert main(["--check"]) == 1

    printed = capsys.readouterr().out
    assert STATE_SNAPSHOT_RULE_ID in printed
    assert f"add {RULE_ID}" not in printed


def test_apply_replaces_its_own_rule_and_honours_days(wire, provider):
    assert main(["--apply", "--days", "7"]) == 0
    assert main(["--apply", "--days", "14"]) == 0

    rules = stored_rules(provider)
    assert list(rules) == [RULE_ID, STATE_SNAPSHOT_RULE_ID]
    assert rules[STATE_SNAPSHOT_RULE_ID]["NoncurrentVersionExpiration"]["NoncurrentDays"] == 1
    rule = rules[RULE_ID]
    assert rule["Status"] == "Enabled"
    assert rule["Filter"] == {"Prefix": "agents/"}
    assert rule["NoncurrentVersionExpiration"]["NoncurrentDays"] == 14
    assert rule["Expiration"]["ExpiredObjectDeleteMarker"] is True
    assert rule["AbortIncompleteMultipartUpload"]["DaysAfterInitiation"] == 7


@pytest.mark.parametrize("configured", ["agents", "/agents/", "agents//"])
def test_the_prefix_is_normalised_like_the_storage_backend(wire, provider, monkeypatch, configured):
    point_settings(monkeypatch, wire, S3_PREFIX=configured)

    assert main(["--apply"]) == 0

    assert stored_rules(provider)[RULE_ID]["Filter"] == {"Prefix": "agents/"}


def test_an_empty_prefix_means_the_whole_bucket(wire, monkeypatch):
    wire.state.versioning = "Enabled"
    assert main(["--apply"]) == 0
    point_settings(monkeypatch, wire, S3_PREFIX="")
    assert main(["--check"]) == 1

    assert main(["--apply"]) == 0

    assert main(["--check"]) == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"STORAGE_BACKEND": "local"},
        {"S3_ENDPOINT_URL": "https://storage.googleapis.com"},
        {"S3_BUCKET": ""},
    ],
    ids=["local-storage", "gcs-endpoint", "no-bucket"],
)
@pytest.mark.parametrize("argv", [["--check"], ["--apply"]])
def test_unsupported_settings_exit_2_without_touching_the_provider(wire, monkeypatch, capsys, overrides, argv):
    point_settings(monkeypatch, wire, **overrides)

    assert main(argv) == 2

    assert capsys.readouterr().err
    assert wire.state.calls == []


@pytest.mark.parametrize(
    ("argv", "refused"),
    [(["--check"], "versioning"), (["--check"], "lifecycle"), (["--apply"], "lifecycle")],
    ids=["check-versioning", "check-lifecycle", "apply-lifecycle"],
)
def test_a_provider_error_exits_2_with_its_code(wire, capsys, argv, refused):
    wire.state.versioning = "Enabled"
    wire.state.refused_bucket_requests = {refused}

    assert main(argv) == 2

    assert "AccessDenied" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["--days", "5"], ["--apply", "--days", "0"], ["--apply", "--check"]])
def test_usage_errors_exit_2(argv):
    with pytest.raises(SystemExit) as raised:
        main(argv)
    assert raised.value.code == 2
