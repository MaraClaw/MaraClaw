"""Check or apply the S3 lifecycle rule that expires noncurrent object versions under the engine's agents prefix."""

import argparse
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Final, NotRequired, Protocol, TypedDict, TypeIs, Unpack

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.config import Settings, get_settings
from app.services.storage_runtime.utils import normalize_storage_key

RULE_ID: Final = "maraclaw-agent-noncurrent-versions"
DEFAULT_DAYS: Final = 30
ABORT_MULTIPART_DAYS: Final = 7

EPILOG: Final = f"""\
The rule covers the engine's agents prefix, <S3_PREFIX>/ (S3_PREFIX normalised like the storage backend). An empty
S3_PREFIX means an empty filter: the rule then applies to the whole bucket, objects the engine does not own included.

--check (the default) exits 0 when bucket versioning is off (no Status), or when an Enabled rule with
NoncurrentVersionExpiration applies to every key under the prefix: its prefix (Filter.Prefix, the legacy top-level
Prefix, Filter.And.Prefix without tags or size bounds, or an empty filter) must be a prefix of <S3_PREFIX>/. A tag or
size filter narrows a rule and so does not cover. A Suspended bucket still keeps noncurrent versions, so it is checked
like an Enabled one. Exit 1 prints why no rule covers the prefix.

--apply puts the rule {RULE_ID}: Filter Prefix <S3_PREFIX>/,
NoncurrentVersionExpiration after --days (default {DEFAULT_DAYS}), expired delete markers removed and incomplete
multipart uploads aborted after {ABORT_MULTIPART_DAYS} days. It keeps every other rule exactly as it is and replaces
a rule with the same ID. It does not read the versioning status, so it also works before versioning is turned on.

Exit codes: 0 covered, versioning off, or applied; 1 not covered (--check); 2 unsupported settings (STORAGE_BACKEND
is not s3, no S3_BUCKET, a Google Cloud Storage endpoint, which has no S3 lifecycle API), an S3 error (its code is
printed, for example AccessDenied) or a usage error.

IAM: --check needs s3:GetBucketVersioning and s3:GetLifecycleConfiguration; --apply needs
s3:GetLifecycleConfiguration and s3:PutLifecycleConfiguration.
"""


class RuleTag(TypedDict):
    Key: str
    Value: str


class RuleAnd(TypedDict, total=False):
    Prefix: str
    Tags: list[RuleTag]
    ObjectSizeGreaterThan: int
    ObjectSizeLessThan: int


class RuleFilter(TypedDict, total=False):
    Prefix: str
    Tag: RuleTag
    ObjectSizeGreaterThan: int
    ObjectSizeLessThan: int
    And: RuleAnd


class Expiration(TypedDict, total=False):
    Days: int
    Date: datetime
    ExpiredObjectDeleteMarker: bool


class NoncurrentVersionExpiration(TypedDict, total=False):
    NoncurrentDays: int
    NewerNoncurrentVersions: int


class AbortIncompleteMultipartUpload(TypedDict):
    DaysAfterInitiation: int


class LifecycleRule(TypedDict, total=False):
    ID: str
    Status: str
    Prefix: str
    Filter: RuleFilter
    Expiration: Expiration
    NoncurrentVersionExpiration: NoncurrentVersionExpiration
    AbortIncompleteMultipartUpload: AbortIncompleteMultipartUpload
    Transitions: list[Mapping[str, object]]
    NoncurrentVersionTransitions: list[Mapping[str, object]]


class LifecycleConfiguration(TypedDict):
    Rules: list[LifecycleRule]


class BucketArguments(TypedDict):
    Bucket: str


class VersioningResponse(TypedDict):
    Status: NotRequired[str]


class LifecycleResponse(TypedDict):
    Rules: NotRequired[list[LifecycleRule]]
    TransitionDefaultMinimumObjectSize: NotRequired[str]


class PutLifecycleArguments(TypedDict):
    Bucket: str
    LifecycleConfiguration: LifecycleConfiguration
    TransitionDefaultMinimumObjectSize: NotRequired[str]


class LifecycleClient(Protocol):
    def get_bucket_versioning(self, **kwargs: Unpack[BucketArguments]) -> VersioningResponse: ...

    def get_bucket_lifecycle_configuration(self, **kwargs: Unpack[BucketArguments]) -> LifecycleResponse: ...

    def put_bucket_lifecycle_configuration(self, **kwargs: Unpack[PutLifecycleArguments]) -> object: ...


class Arguments(argparse.Namespace):
    check: bool = False
    apply: bool = False
    days: int | None = None


def _where(prefix: str) -> str:
    return f"under {prefix!r}" if prefix else "in the whole bucket"


def _has_size_bound(scope: Mapping[str, object]) -> bool:
    return "ObjectSizeGreaterThan" in scope or "ObjectSizeLessThan" in scope


def _rule_prefix(rule: LifecycleRule) -> str | None:
    """The key prefix a rule applies to, or None when a tag or size bound limits it to some of those keys."""
    rule_filter = rule.get("Filter")
    if rule_filter is None:
        return rule.get("Prefix", "")
    and_filter = rule_filter.get("And")
    if and_filter is not None:
        return None if bool(and_filter.get("Tags")) or _has_size_bound(and_filter) else and_filter.get("Prefix", "")
    return None if "Tag" in rule_filter or _has_size_bound(rule_filter) else rule_filter.get("Prefix", "")


def covers(rules: Sequence[LifecycleRule], prefix: str) -> str | None:
    """None when an enabled rule expires noncurrent versions of every key under ``prefix``, else why none does."""
    expiring = [rule for rule in rules if rule.get("Status") == "Enabled" and "NoncurrentVersionExpiration" in rule]
    for rule in expiring:
        rule_prefix = _rule_prefix(rule)
        if rule_prefix is not None and prefix.startswith(rule_prefix):
            return None
    if not expiring:
        return f"no enabled lifecycle rule expires noncurrent versions {_where(prefix)}"
    return (
        f"no enabled noncurrent-version rule applies to every key {_where(prefix)}: "
        "each is limited to a narrower prefix, to tags or to an object size"
    )


def merge(rules: Sequence[LifecycleRule], prefix: str, days: int) -> list[LifecycleRule]:
    """``rules`` plus the agent rule, which replaces the rule with its ID; every other rule is kept as it is."""
    agent_rule: LifecycleRule = {
        "ID": RULE_ID,
        "Status": "Enabled",
        "Filter": {"Prefix": prefix} if prefix else {},
        "NoncurrentVersionExpiration": {"NoncurrentDays": days},
        "Expiration": {"ExpiredObjectDeleteMarker": True},
        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": ABORT_MULTIPART_DAYS},
    }
    if not any(rule.get("ID") == RULE_ID for rule in rules):
        return [*rules, agent_rule]
    return [agent_rule if rule.get("ID") == RULE_ID else rule for rule in rules]


def lifecycle_prefix(s3_prefix: str) -> str:
    """The rule's filter prefix for ``S3_PREFIX``: the normalised prefix and a slash, or "" when it is empty."""
    normalized = normalize_storage_key(s3_prefix)
    return f"{normalized}/" if normalized else ""


def _unsupported(settings: Settings) -> str | None:
    if (settings.STORAGE_BACKEND or "local").strip().lower() != "s3":
        return "STORAGE_BACKEND is not s3, so there is no bucket to manage"
    if "storage.googleapis.com" in settings.S3_ENDPOINT_URL:
        return "S3_ENDPOINT_URL is Google Cloud Storage, which has no S3 lifecycle API; configure its lifecycle there"
    if not settings.S3_BUCKET:
        return "S3_BUCKET is empty"
    return None


def _is_lifecycle_client(value: object) -> TypeIs[LifecycleClient]:
    return value is not None


def _client(settings: Settings) -> LifecycleClient:
    """The client ``S3Connection`` builds for these settings: path-style addressing, SigV4 and no proxies."""
    config = Config(
        proxies={},
        s3={"addressing_style": "path"},
        signature_version="s3v4",
        connect_timeout=5,
        read_timeout=30,
        region_name=settings.S3_REGION or None,
    )
    client = boto3.Session().client(
        "s3",
        endpoint_url=settings.S3_ENDPOINT_URL or None,
        aws_access_key_id=settings.S3_ACCESS_KEY_ID or None,
        aws_secret_access_key=settings.S3_SECRET_ACCESS_KEY or None,
        config=config,
    )
    if not _is_lifecycle_client(client):
        raise RuntimeError("boto3 S3 client is missing required methods")
    return client


def _lifecycle_configuration(client: LifecycleClient, bucket: str) -> LifecycleResponse:
    """The bucket's lifecycle configuration; a bucket with none has no rules."""
    try:
        return client.get_bucket_lifecycle_configuration(Bucket=bucket)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "NoSuchLifecycleConfiguration":
            return {}
        raise


def check(client: LifecycleClient, bucket: str, prefix: str) -> int:
    status = client.get_bucket_versioning(Bucket=bucket).get("Status", "")
    if not status:
        print("OK: bucket versioning is off, so there are no noncurrent versions to expire")
        return 0
    reason = covers(_lifecycle_configuration(client, bucket).get("Rules", []), prefix)
    if reason is not None:
        print(f"FAIL: bucket versioning is {status} and {reason}; run with --apply to add {RULE_ID}")
        return 1
    print(f"OK: bucket versioning is {status} and noncurrent versions {_where(prefix)} expire")
    return 0


def apply(client: LifecycleClient, bucket: str, prefix: str, days: int) -> int:
    current = _lifecycle_configuration(client, bucket)
    arguments: PutLifecycleArguments = {
        "Bucket": bucket,
        "LifecycleConfiguration": {"Rules": merge(current.get("Rules", []), prefix, days)},
    }
    minimum_object_size = current.get("TransitionDefaultMinimumObjectSize")
    if minimum_object_size is not None:
        arguments["TransitionDefaultMinimumObjectSize"] = minimum_object_size
    _ = client.put_bucket_lifecycle_configuration(**arguments)
    print(f"Applied {RULE_ID}: noncurrent versions {_where(prefix)} expire after {days} days")
    return 0


def _positive_days(value: str) -> int:
    days = int(value)
    if days < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return days


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.s3_lifecycle",
        description=__doc__,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    _ = mode.add_argument("--check", action="store_true", help="report whether the rule is needed (the default)")
    _ = mode.add_argument("--apply", action="store_true", help="put or merge the rule")
    _ = parser.add_argument(
        "--days",
        type=_positive_days,
        help=f"with --apply: days a noncurrent version is kept (default {DEFAULT_DAYS})",
    )
    args = parser.parse_args(argv, namespace=Arguments())
    if args.days is not None and not args.apply:
        parser.error("--days needs --apply")
    settings = get_settings()
    problem = _unsupported(settings)
    if problem is not None:
        print(f"error: {problem}", file=sys.stderr)
        return 2
    prefix = lifecycle_prefix(settings.S3_PREFIX)
    try:
        client = _client(settings)
        if args.apply:
            return apply(client, settings.S3_BUCKET, prefix, args.days or DEFAULT_DAYS)
        return check(client, settings.S3_BUCKET, prefix)
    except ClientError as exc:
        error = exc.response.get("Error", {})
        print(
            f"error: S3 {exc.operation_name} failed: {error.get('Code', '')} {error.get('Message', '')}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
