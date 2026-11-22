"""Typed views of the untyped boto3/botocore surface: client construction and ``ClientError`` payloads."""

from typing import Protocol, TypedDict, Unpack

from botocore.config import Config


class ClientErrorDetail(TypedDict, total=False):
    Code: str
    Message: str


class ClientErrorMetadata(TypedDict, total=False):
    HTTPStatusCode: int


class ClientErrorResponse(TypedDict, total=False):
    """``ClientError.response`` as botocore parses it; a provider's answer can omit any key."""

    Error: ClientErrorDetail
    ResponseMetadata: ClientErrorMetadata


class _ClientErrorLike(Protocol):
    @property
    def response(self) -> ClientErrorResponse: ...


def client_error_response(exc: _ClientErrorLike) -> ClientErrorResponse:
    """The parsed error payload of a ``botocore.exceptions.ClientError``."""
    return exc.response


class S3ClientOptions(TypedDict, total=False):
    region_name: str | None
    endpoint_url: str | None
    aws_access_key_id: str | None
    aws_secret_access_key: str | None
    config: Config


class S3ClientFactory(Protocol):
    """The ``boto3`` module or a ``boto3.Session``: each builds a client for a service name."""

    def client(
        self,
        service_name: str,
        *,
        region_name: str | None = None,
        endpoint_url: str | None = None,
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
        config: Config | None = None,
    ) -> object: ...


def new_s3_client(factory: S3ClientFactory, **options: Unpack[S3ClientOptions]) -> object:
    """An S3 client built with exactly ``options``; callers narrow it to the operations they call."""
    return factory.client("s3", **options)
