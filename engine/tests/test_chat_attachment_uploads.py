from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import quote

import pytest
from fastapi import UploadFile

from app.api import upload
from app.api.file_previews import PreviewMetadata
from app.records.chat_attachment import ChatAttachment
from app.services import chat_submissions
from app.services.storage_runtime import facade
from file_preview_fixtures import PreviewContext, preview_context as preview_context
from openclaw_update_gate_fixtures import open_guest_write_gate
from test_websocket_turn_transaction import TurnConnection, TurnState, drive_turn, turn as turn


@pytest.mark.parametrize(
    "filename",
    [
        "Report: Q3.pdf",
        "a:b.pdf",
        " lead.pdf",
        "q.pdf ",
        "گزارش\u200c.pdf",
        "👩\u200d💻.pdf",
        "\u2060report.pdf",
    ],
    ids=["colon", "single-letter-colon", "leading-space", "trailing-space", "zwnj", "zwj", "format-character"],
)
async def test_real_upload_roundtrips_attachment_submission_and_preview(
    monkeypatch: pytest.MonkeyPatch,
    turn: tuple[TurnState, TurnConnection],
    preview_context: PreviewContext,
    filename: str,
) -> None:
    # Given: real upload storage/extraction, with only authorization and the guest gate isolated.
    state, _raw = turn
    ctx = preview_context
    content = (Path(__file__).parent / "fixtures" / "anydoc" / "supported.pdf").read_bytes()
    open_guest_write_gate(monkeypatch)
    monkeypatch.setattr(upload, "check_agent_access", AsyncMock(return_value=(state.agent, "use")))
    monkeypatch.setattr(upload, "get_storage_backend", lambda: ctx.storage)
    monkeypatch.setattr(facade, "get_storage_backend", lambda: ctx.storage)
    monkeypatch.setattr(chat_submissions, "get_storage_backend", lambda: ctx.storage)
    with BytesIO(content) as stream:
        uploaded = await upload.upload_file(
            file=UploadFile(stream, filename=filename), agent_id=str(state.agent.id), current_user=state.user
        )

    # When: the actual returned reference crosses chat validation and authenticated preview transport.
    attachment = ChatAttachment(path=uploaded["workspace_path"], name=uploaded["filename"])
    await drive_turn(
        monkeypatch,
        state,
        payload={
            "content": "Review this",
            "client_submission_id": state.submission_id,
            "attachments": [attachment.as_dict()],
        },
    )
    url = f"/api/agents/{state.agent.id}/files"
    metadata_response = await ctx.client.get(
        f"{url}/preview-metadata", params={"path": attachment.path}, headers=ctx.headers
    )
    assert metadata_response.status_code == 200
    metadata = PreviewMetadata.model_validate(metadata_response.json())
    response = await ctx.client.get(
        f"{url}/preview-bytes",
        params={"path": attachment.path, "version_token": metadata.version_token},
        headers=ctx.headers,
    )

    # Then: original names/paths survive persistence and both preview responses refer to the uploaded bytes.
    expected = {"path": f"workspace/uploads/{filename}", "name": filename}
    assert attachment.as_dict() == expected
    assert state.frames[0]["type"] == "turn_accepted"
    refs = state.messages[0].attachments
    assert refs is not None
    assert [ref.as_dict() for ref in refs] == [expected]
    assert (metadata.path, metadata.name, metadata.size) == (expected["path"], filename, len(content))
    assert response.status_code == 200
    assert response.content == content
    assert response.headers["content-disposition"] == f"attachment; filename*=UTF-8''{quote(filename, safe='')}"
