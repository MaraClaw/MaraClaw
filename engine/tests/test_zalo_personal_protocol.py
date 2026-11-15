import base64
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from cryptography.exceptions import InvalidTag
from pydantic import ValidationError

from app.records.zalo_personal import ZaloRoute
from app.services.channels.whatsapp_personal_crypto import AuthCipher, AuthKey
from app.services.channels.zalo_personal_protocol import child_frame


def test_route_roundtrips_strict_fences_and_refuses_overflow():
    route = ZaloRoute(config_id=uuid4(), auth_epoch=uuid4(), generation=uuid4(), account_id="123", peer_id="456")
    assert ZaloRoute.from_external_id(route.external_id()) == route
    for invalid in [
        route.external_id() + ":extra",
        route.external_id().upper(),
        route.external_id() + "x",
        route.external_id().replace("123:456", "123:group"),
    ]:
        with pytest.raises(ValueError):
            ZaloRoute.from_external_id(invalid)
    with pytest.raises(ValueError, match="oversize"):
        route.model_copy(update={"account_id": "1" * 64, "peer_id": "2" * 64}).external_id()


def test_cipher_default_is_compatible_and_zalo_purpose_cannot_cross_decrypt():
    key = AuthKey(uuid4(), uuid4(), "creds", "main")
    sealed = AuthCipher("test").seal(key, "secret")
    assert AuthCipher("test", purpose=b"maraclaw:whatsapp:auth:v1").open(key, sealed) == "secret"
    with pytest.raises(InvalidTag):
        AuthCipher("test", purpose=b"maraclaw:zalo:auth:v1").open(key, sealed)


def test_concrete_child_contract_rejects_old_frames_and_malformed_png():
    common = {"id": "frame", "token": "token"}
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()
    valid = {**common, "type": "qr", "value": png, "expires_at": datetime.now(UTC).isoformat()}
    assert child_frame.validate_json(json.dumps(valid)).type == "qr"
    for invalid in [
        {**valid, "value": "data:image/png;base64," + png},
        {**valid, "value": "A" * 768001},
        {**common, "type": "auth_get", "category": "creds", "ids": ["main"]},
        {**common, "type": "identity", "account_id": "not-numeric"},
        {**common, "type": "ready", "extra": 1},
    ]:
        with pytest.raises(ValidationError):
            child_frame.validate_json(json.dumps(invalid))
