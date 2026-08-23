"""File upload API for chat - saves files to agent workspace and extracts text."""

import asyncio
import base64
import os
import uuid
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.records.user import UserRecord
from app.services.document_parser import (
    IMAGE_UPLOAD_MAX_BYTES,
    MAX_INPUT_BYTES,
    OFFICE_EXTENSIONS,
    TEXT_EXTENSIONS,
    convert_document_isolated,
    decode_text_bytes,
    format_parse_failure,
    needs_extraction,
    read_bytes_limited,
)
from app.services.document_parser.errors import DocumentParseError, DocumentTooLargeError
from app.services.storage import ensure_local_path, get_storage_backend, guess_content_type, normalize_storage_key

router = APIRouter(prefix="/chat", tags=["chat"])

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
EXTRACTABLE = TEXT_EXTENSIONS | OFFICE_EXTENSIONS

MIME_MAP = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


def validate_upload_filename(filename: str) -> str:
    """Accept a single filename, never a client-provided storage path."""
    if filename in {"", ".", ".."} or "/" in filename or "\\" in filename:
        raise ValueError("filename must not contain path components")
    return filename


def extract_text(content: bytes, filename: str, extension: str) -> str:
    """Extract text from upload bytes. Ordinary text stays local; office files use anydoc."""
    if extension in TEXT_EXTENSIONS:
        return decode_text_bytes(content)

    if not needs_extraction(filename):
        return f"[Unsupported file format: {extension}]"

    try:
        extracted = convert_document_isolated(content, filename)
    except DocumentParseError as exc:
        return _upload_extract_failure(extension, exc)
    if extracted:
        return extracted
    return _upload_extract_failure(extension, None)


def _upload_extract_failure(extension: str, exc: DocumentParseError | None) -> str:
    detail = format_parse_failure(exc) if exc is not None else f"Unsupported file format: {extension}"
    if extension == ".pdf":
        return f"[PDF text extraction failed] {detail}" if exc is not None else "[PDF text extraction failed]"
    if extension in {".docx", ".doc", ".docm"}:
        return f"[DOCX text extraction failed] {detail}" if exc is not None else "[DOCX text extraction failed]"
    if extension in {".xlsx", ".xls", ".xlsm", ".xlsb"}:
        return f"[Excel text extraction failed] {detail}" if exc is not None else "[Excel text extraction failed]"
    if exc is not None:
        return f"[{detail}]"
    return f"[Unsupported file format: {extension}]"


@router.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    agent_id: str = Form(""),
    current_user: UserRecord = Depends(get_current_user),
) -> dict[str, Any]:
    """Upload a file for chat context. Saves to agent workspace/uploads/ and returns extracted text."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename")

    try:
        filename = validate_upload_filename(file.filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    ext = os.path.splitext(filename)[1].lower()
    is_image = ext in IMAGE_EXTENSIONS
    max_bytes = IMAGE_UPLOAD_MAX_BYTES if is_image else MAX_INPUT_BYTES
    try:
        content = await read_bytes_limited(file.read, max_bytes)
    except DocumentTooLargeError as exc:
        raise HTTPException(status_code=400, detail=format_parse_failure(exc)) from exc

    # Determine save directory
    workspace_path = ""
    if agent_id:
        try:
            agent_uuid = uuid.UUID(agent_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid agent_id") from exc
        _ = await check_agent_access(current_user, agent_uuid)
        storage = get_storage_backend()
        workspace_path = f"workspace/uploads/{filename}"
        key = normalize_storage_key(f"{agent_id}/{workspace_path}")
        counter = 1
        while await storage.exists(key):
            stem, ext = os.path.splitext(filename)
            filename = f"{stem}_{counter}{ext}"
            workspace_path = f"workspace/uploads/{filename}"
            key = normalize_storage_key(f"{agent_id}/{workspace_path}")
            counter += 1
        await storage.write_bytes(key, content, content_type=guess_content_type(filename))
        _ = await ensure_local_path(key)
        saved_filename = filename
    else:
        # No agent workspace exists to retain this upload. Keep its bytes request-scoped.
        file_id = str(uuid.uuid4())[:8]
        saved_filename = f"{file_id}_{filename}"

    image_data_url = ""
    if is_image:
        mime = MIME_MAP.get(ext, "image/png")
        b64 = base64.b64encode(content).decode("ascii")
        image_data_url = f"data:{mime};base64,{b64}"
        extracted = f"[Image file: {filename}; visual model analysis required]"
    elif ext in EXTRACTABLE:
        extracted = await asyncio.to_thread(extract_text, content, filename, ext)
    else:
        extracted = (
            f"[File saved. Text extraction is not supported for {ext}; the agent can use the read_document tool.]"
        )

    # Truncate if too long
    if len(extracted) > 6000:
        extracted = extracted[:6000] + "\n\n...[content truncated; " + str(len(extracted)) + " characters total]"

    return {
        "filename": filename,
        "saved_filename": saved_filename,
        "size": len(content),
        "extracted_text": extracted,
        "workspace_path": workspace_path,
        "is_image": is_image,
        "image_data_url": image_data_url,
    }
