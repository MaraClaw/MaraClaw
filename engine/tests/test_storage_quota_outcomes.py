import asyncio
from uuid import uuid4

import pytest
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from app.services.storage_runtime.base import AtomicCreateUnsupportedError, StorageConditionalRaceError
from app.services.storage_runtime.quota_errors import ManagedKeyError
from app.services.storage_runtime.quota_keys import ManagedKey, parse_managed_key
from app.services.storage_runtime.quota_outcomes import Outcome, failure_outcome


def client_error(status: int) -> ClientError:
    return ClientError({"Error": {"Code": "X"}, "ResponseMetadata": {"HTTPStatusCode": status}}, "PutObject")


@pytest.mark.parametrize(
    "failure",
    [
        client_error(403),
        client_error(412),
        client_error(409),
        EndpointConnectionError(endpoint_url="http://s3.invalid"),
        ConnectTimeoutError(endpoint_url="http://s3.invalid", error="no route"),
        AtomicCreateUnsupportedError("S3", "key"),
        StorageConditionalRaceError("key", 3),
    ],
)
def test_failures_that_cannot_have_changed_the_provider_are_rejections(failure: BaseException):
    assert failure_outcome(failure) is Outcome.REJECTED


@pytest.mark.parametrize(
    "failure",
    [
        client_error(500),
        client_error(503),
        ReadTimeoutError(endpoint_url="http://s3.invalid", error="no response"),
        ConnectionClosedError(endpoint_url="http://s3.invalid"),
        OSError("reset by peer"),
        asyncio.CancelledError(),
        RuntimeError("unexpected"),
    ],
)
def test_failures_that_may_have_been_applied_stay_unknown(failure: BaseException):
    assert failure_outcome(failure) is Outcome.UNKNOWN


def test_agent_keys_map_to_their_agent_and_relative_path():
    agent = uuid4()
    assert parse_managed_key(f"{agent}/workspace/uploads/a.txt") == ManagedKey(agent, "workspace/uploads/a.txt")
    assert parse_managed_key(f"/{agent}") == ManagedKey(agent, "")


def test_traversal_cannot_move_a_key_out_of_the_agent_it_is_charged_to():
    first, second = uuid4(), uuid4()
    assert parse_managed_key(f"{first}/../{second}/x") == ManagedKey(second, "x")
    assert parse_managed_key(f"{first}/a/../../x") is None


@pytest.mark.parametrize("key", [f"enterprise_info_{uuid4()}/doc.txt", "plain/file", "", "skills/shared"])
def test_non_agent_keys_are_not_managed(key: str):
    assert parse_managed_key(key) is None


@pytest.mark.parametrize(
    "spelling",
    [lambda u: str(u).upper(), lambda u: u.hex, lambda u: f"{{{u}}}", lambda u: f"urn:uuid:{u}"],
    ids=["upper", "hex", "braces", "urn"],
)
def test_uuid_lookalike_prefixes_fail_closed_instead_of_aliasing_an_agent(spelling):
    with pytest.raises(ManagedKeyError):
        parse_managed_key(f"{spelling(uuid4())}/x")
