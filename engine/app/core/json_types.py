"""Shared JSON value annotations for persisted configuration and event data."""

type JsonValue = str | int | float | bool | list[JsonValue] | dict[str, JsonValue] | None
type JsonObject = dict[str, JsonValue]
