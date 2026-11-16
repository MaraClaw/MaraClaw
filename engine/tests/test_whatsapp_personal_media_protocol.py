import base64
import json
from pathlib import Path
from typing import Annotated

import anyio
from pydantic import Field, TypeAdapter, ValidationError

from app.services.channels.whatsapp_personal_protocol import (
    MediaAck,
    MediaChunk,
    MediaEnd,
    MediaError,
    MediaGet,
    Message,
)

BRIDGE = Path(__file__).parents[1] / "connectors/whatsapp"
FRAMES = TypeAdapter(
    Annotated[MediaAck | MediaChunk | MediaEnd | MediaError | MediaGet | Message, Field(discriminator="type")]
)


async def test_private_media_protocol_mirrors_agree_on_boundary_values_and_unknown_fields():
    envelope = {"id": "frame", "token": "capability"}
    offer = {
        "source_id": "a" * 64,
        "kind": "file",
        "filename": "report.docx",
        "content_type": "application/octet-stream",
        "size": 0,
    }
    message = {
        **envelope,
        "type": "message",
        "message_id": "native",
        "remote_jid": "self@s.whatsapp.net",
        "participant": None,
        "from_me": True,
        "timestamp": 1,
        "text": "",
        "event_kind": "notify",
        "is_history": False,
        "media": offer,
    }
    chunk = {**envelope, "type": "media_chunk", "transfer_id": "transfer", "seq": 0, "data": "YQ=="}
    get = {
        **envelope,
        "type": "media_get",
        "transfer_id": "transfer",
        "message_id": "native",
        "source_id": "a" * 64,
        "limit": 52428800,
    }
    end = {**envelope, "type": "media_end", "transfer_id": "transfer", "seq": 1, "size": 1, "sha256": "f" * 64}
    cases = [
        (message, True),
        ({**message, "media": {**offer, "kind": "image"}}, True),
        ({**message, "media": {**offer, "kind": "video"}}, True),
        ({**message, "text": "😀" * 4096, "media": None}, True),
        ({**message, "text": "", "media": None}, False),
        ({**message, "text": "😀" * 4097}, False),
        ({**message, "media": {**offer, "url": "https://provider.invalid/private"}}, False),
        ({**message, "media": {**offer, "size": True}}, False),
        ({**message, "media": {**offer, "size": 9007199254740992}}, False),
        ({**message, "media": {**offer, "source_id": "not-opaque"}}, False),
        ({**message, "media": {**offer, "source_id": "a" * 64 + "\n"}}, False),
        ({**message, "media": {**offer, "filename": "😀" * 1024}}, True),
        ({**message, "media": {**offer, "filename": "😀" * 1025}}, False),
        ({**message, "is_history": True}, False),
        (chunk, True),
        ({**chunk, "data": base64.b64encode(b"x" * 65536).decode()}, True),
        ({**chunk, "data": base64.b64encode(b"x" * 65537).decode()}, False),
        ({**chunk, "data": "YR=="}, False),
        ({**chunk, "data": "YQ==\n"}, False),
        ({**chunk, "data": ""}, False),
        ({**chunk, "seq": -1}, False),
        ({**chunk, "seq": True}, False),
        ({**chunk, "unexpected": "private"}, False),
        (get, True),
        ({**get, "limit": 52428801}, False),
        ({**get, "limit": 0}, False),
        ({**get, "url": "private"}, False),
        (end, True),
        ({**end, "size": 0}, False),
        ({**end, "sha256": "f" * 63}, False),
        ({**envelope, "type": "media_ack", "transfer_id": "transfer", "seq": 0}, True),
        ({**envelope, "type": "media_error", "transfer_id": "transfer", "code": "expired"}, True),
        ({**envelope, "type": "media_error", "transfer_id": "transfer", "code": "secret error"}, False),
    ]
    expected = [accepted for _, accepted in cases]
    python = []
    for frame, _ in cases:
        try:
            _ = FRAMES.validate_json(json.dumps(frame))
        except ValidationError:
            python.append(False)
        else:
            python.append(True)
    with anyio.fail_after(10):
        node = await anyio.run_process(
            ["node", str(BRIDGE / "dist/test/media-mirror.js")],
            input=json.dumps([frame for frame, _ in cases]).encode(),
        )
    assert python == expected
    assert json.loads(node.stdout) == expected
