import json
from collections.abc import Mapping
from typing import Literal

from app.core.json_types import JsonValue, json_loads_value, json_object_from, str_from_row, uuid_from_row
from app.core.stored_credentials import (
    CredentialContext,
    PreservedCredential,
    StoredCredentialError,
    open_credential,
    seal_credential,
)

type CredentialOperation = Literal["seal", "open", "metadata"]
_SECRET_COLUMNS = ("app_secret", "encrypt_key", "verification_token")


def _secret(value: str, context: CredentialContext, operation: CredentialOperation) -> str:
    if not value:
        return value
    match operation:
        case "metadata":
            return PreservedCredential(value, context)
        case "open":
            return open_credential(value, context)
        case "seal":
            return seal_credential(value, context)


def channel_credentials(row: Mapping[str, object], operation: CredentialOperation) -> dict[str, object]:
    agent = str(uuid_from_row(row["agent_id"]))
    record = str(uuid_from_row(row["id"]))
    channel = str_from_row(row["channel_type"])

    def context(path: tuple[str, ...]) -> CredentialContext:
        return CredentialContext("channel", agent, record, path, channel)

    def walk(value: JsonValue, path: tuple[str, ...], sensitive: bool = False) -> JsonValue:
        if isinstance(value, dict):
            return {
                key: walk(
                    item,
                    (*path, key),
                    sensitive
                    or key.lower() == "service_account_json"
                    or any(part in key.lower() for part in ("secret", "private", "token")),
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [walk(item, (*path, str(index)), sensitive) for index, item in enumerate(value)]
        if sensitive and value not in (None, ""):
            binding = context(path)
            match operation:
                case "seal":
                    return seal_credential(
                        value if isinstance(value, PreservedCredential) else json.dumps(value), binding
                    )
                case "metadata":
                    return PreservedCredential(str(value), binding)
                case "open":
                    if not isinstance(value, str):
                        raise StoredCredentialError
                    try:
                        decoded = json_loads_value(open_credential(value, binding))
                    except ValueError:
                        raise StoredCredentialError from None
                    if isinstance(decoded, (str, int, float, bool)):
                        return decoded
                    raise StoredCredentialError
        return value

    result = dict(row)
    for column in _SECRET_COLUMNS:
        value = row.get(column)
        if value is not None:
            result[column] = _secret(str_from_row(value), context((column,)), operation)
    if "extra_config" in row:
        result["extra_config"] = walk(json_object_from(row["extra_config"] or {}), ("extra_config",))
    return result
