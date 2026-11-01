from uuid import uuid4

import pytest

from app.records.whatsapp_personal import PERSONAL_ROUTE_MAX_LENGTH, PersonalRoute, PersonalRouteError

_PN = "15550001@s.whatsapp.net"


def route(account: str = _PN, self_jid: str = _PN) -> PersonalRoute:
    return PersonalRoute(
        config_id=uuid4(), auth_epoch=uuid4(), generation=uuid4(), account_id=account, self_jid=self_jid
    )


def compact(source: PersonalRoute, *, version: str = "1", config: str | None = None) -> str:
    return (
        f"whatsapp_personal:{version}:{config or source.config_id.hex}:{source.auth_epoch.hex}:"
        f"{source.generation.hex}:{source.account_id}:{source.self_jid}"
    )


@pytest.mark.parametrize("self_jid", [_PN, "999@lid", "15550001:2@s.whatsapp.net", ""])
def test_compact_route_roundtrips_every_fence(self_jid):
    # Given a route with distinct config, epoch and generation.
    source = route(self_jid=self_jid)
    # When it is encoded and decoded.
    encoded = source.external_id()
    # Then the compact form fits the deployed bound and every field is restored exactly.
    assert encoded == compact(source)
    assert len(encoded) <= PERSONAL_ROUTE_MAX_LENGTH
    assert PersonalRoute.from_external_id(encoded) == source


def test_legacy_prefixed_json_route_stays_readable_beyond_compact_bound():
    # Given an already persisted prefixed-JSON route, which exceeds the deployed bound.
    source = route()
    legacy = "whatsapp_personal:" + source.model_dump_json()
    assert len(legacy) > PERSONAL_ROUTE_MAX_LENGTH
    # When the shared reader parses it.
    # Then it is accepted unchanged, with no compact-length restriction.
    assert PersonalRoute.from_external_id(legacy) == source
    long_account = route(account="1" * 120 + "@s.whatsapp.net")
    assert PersonalRoute.from_external_id("whatsapp_personal:" + long_account.model_dump_json()) == long_account


def test_encoded_length_bound_is_exact():
    # Given account and self fields that together fill exactly the remaining budget.
    fixed = len(compact(route("", "")))
    room = PERSONAL_ROUTE_MAX_LENGTH - fixed
    # When the route is exactly at the bound, then it encodes and decodes.
    at_bound = route("a" * (room // 2), "b" * (room - room // 2))
    assert len(at_bound.external_id()) == PERSONAL_ROUTE_MAX_LENGTH
    assert PersonalRoute.from_external_id(at_bound.external_id()) == at_bound
    # When one character more would be needed, then encoding is rejected rather than truncated.
    with pytest.raises(PersonalRouteError) as exc:
        route("a" * (room // 2), "b" * (room - room // 2 + 1)).external_id()
    assert exc.value.reason == "oversize"
    with pytest.raises(PersonalRouteError) as exc:
        PersonalRoute.from_external_id(compact(route("a" * (room // 2), "b" * (room - room // 2 + 1))))
    assert exc.value.reason == "oversize"


def test_account_containing_separator_cannot_be_encoded():
    # Given an account that would make the self field ambiguous.
    with pytest.raises(PersonalRouteError) as exc:
        route(account="15550001:2@s.whatsapp.net").external_id()
    assert exc.value.reason == "ambiguous_account"


def test_unknown_version_is_distinct_from_malformed():
    source = route()
    for version in ("2", "10"):
        with pytest.raises(PersonalRouteError) as exc:
            PersonalRoute.from_external_id(compact(source, version=version))
        assert exc.value.reason == "unsupported_version"
    with pytest.raises(PersonalRouteError) as exc:
        PersonalRoute.from_external_id(compact(source, version="v1"))
    assert exc.value.reason == "malformed"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: "",
        lambda s: "whatsapp:" + compact(s).removeprefix("whatsapp_personal:"),
        lambda s: compact(s).rsplit(":", 2)[0],
        lambda s: compact(s, config=str(s.config_id)),
        lambda s: compact(s, config=s.config_id.hex.upper()),
        lambda s: compact(s, config=s.config_id.hex[:-1]),
        lambda s: "whatsapp_personal:{not-json",
        lambda s: "whatsapp_personal:" + s.model_dump_json().replace('"version":1', '"version":2'),
        lambda s: "whatsapp_personal:" + s.model_dump_json()[:-1] + ',"extra":1}',
    ],
)
def test_malformed_routes_are_rejected_without_echoing_input(mutate):
    # Given a corrupted persisted route.
    source = route()
    corrupted = mutate(source)
    # When it is parsed.
    with pytest.raises(PersonalRouteError) as exc:
        PersonalRoute.from_external_id(corrupted)
    # Then the typed ValueError carries only a reason code, never the persisted bytes.
    assert isinstance(exc.value, ValueError)
    assert exc.value.reason in {"malformed", "unsupported_version"}
    assert str(source.config_id.hex) not in str(exc.value)
