import json
import re
from collections.abc import Sequence
from dataclasses import replace
from pathlib import PurePosixPath

from fastapi import HTTPException

from app.core.json_types import JsonObject, json_as_str_or, json_object_from
from app.core.outbound_transport import public_http_client
from app.records.channel_config import ChannelConfigRecord
from app.records.chat_attachment import ChatAttachment
from app.services.storage import store_agent_upload

from .media_limits import (
    MediaBudget,
    MediaDeadlineError,
    MediaTooLargeError,
    deadline_note,
    limit_attachments,
    read_capped,
    skipped_attachments_note,
    too_large_note,
)
from .submissions import ChannelEvent, accept_channel_event, claim_channel_event, is_channel_event_replay


async def accept_slack_event(config: ChannelConfigRecord, body: JsonObject, *, final_retry: bool = False) -> None:
    """Accept a Slack event callback, keyed by the event's ``ts`` or the envelope's ``event_id``.

    Slack sends ``event_id`` on every callback and ``ts`` on every message and app mention, so an event with neither is
    refused with 400 before any claim, download or write. A delivery that finds the event's in-flight claim held raises
    ``ChannelEventInFlightError`` (503) so Slack redelivers it. ``final_retry`` marks Slack's last redelivery, which
    proceeds despite a held claim because nothing follows it.
    """
    event = json_object_from(body.get("event"))
    native_id = json_as_str_or(event.get("ts")) or json_as_str_or(body.get("event_id"))
    if not native_id:
        raise HTTPException(status_code=400, detail="Slack event ID is required")
    channel_id = json_as_str_or(event.get("channel"))
    sender_id = json_as_str_or(event.get("user"))
    files_raw = event.get("files")
    files = [json_object_from(item) for item in files_raw] if isinstance(files_raw, list) else []
    text = re.sub(r"^<@[A-Z0-9]+>\s*", "", json_as_str_or(event.get("text"))).strip()
    native = ChannelEvent(
        config_id=config.id,
        agent_id=config.agent_id,
        provider="slack",
        event_key=json.dumps([body.get("team_id"), channel_id, native_id], separators=(",", ":")),
        sender_id=sender_id,
        conversation_id=f"slack_{channel_id}" if channel_id else f"slack_dm_{sender_id}",
        text=text,
        kind="files" if files else "text",
        media_ids=tuple(json_as_str_or(file.get("id")) for file in files),
        is_group=bool(channel_id) and not channel_id.startswith("D"),
    )
    async with claim_channel_event(native, final_delivery=final_retry):
        if not await is_channel_event_replay(native):
            enriched, attachments = await _fetch_files(config, files, text)
            await accept_channel_event(
                replace(native, attachments=attachments), content=enriched, saved_content=enriched
            )


def _filename(file: JsonObject) -> str:
    return json_as_str_or(file.get("name")) or f"slack_{json_as_str_or(file.get('id'))}.bin"


async def _fetch_files(
    config: ChannelConfigRecord, files: Sequence[JsonObject], text: str
) -> tuple[str, tuple[ChatAttachment, ...]]:
    """Fetch and store the files within the acknowledgement budget; returns the message text with its file notes.

    A file is left out, with every file after it, when the deadline passes before its fetch ends or before its store
    starts. A store that has started runs to completion and is attached.
    """
    enriched = text
    attachments: list[ChatAttachment] = []
    budget = MediaBudget.for_acknowledgement()
    retrieved, skipped = limit_attachments(files)
    for index, file in enumerate(retrieved):
        url = json_as_str_or(file.get("url_private_download")) or json_as_str_or(file.get("url_private"))
        filename = _filename(file)
        try:
            limit = budget.limit_for(filename)
            async with (
                budget.window(),
                public_http_client(timeout=30, follow_redirects=True, origin="https://files.slack.com") as client,
                client.stream("GET", url, headers={"Authorization": f"Bearer {config.app_secret or ''}"}) as response,
            ):
                data = await read_capped(response, limit, budget)
                content_type = str(response.headers.get("content-type") or "")
            budget.check()
        except MediaTooLargeError as error:
            budget.spend(error.read)
            enriched = f"{enriched}\n{too_large_note(filename, error.limit)}".strip()
            continue
        except MediaDeadlineError:
            for omitted in retrieved[index:]:
                enriched = f"{enriched}\n{deadline_note(_filename(omitted))}".strip()
            break
        budget.spend(len(data))
        _, workspace_path, _ = await store_agent_upload(config.agent_id, filename, data, content_type=content_type)
        attachments.append(ChatAttachment(path=workspace_path, name=PurePosixPath(workspace_path).name))
        enriched = f"{enriched}\n[file:{workspace_path}]".strip()
    if skipped:
        enriched = f"{enriched}\n{skipped_attachments_note(skipped)}".strip()
    return enriched, tuple(attachments)
