"""File upload API for chat - saves files to agent workspace and extracts text."""

import asyncio
import base64
import os
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from app.core.security import get_current_user
from app.records.user import UserRecord
from app.services.storage import ensure_local_path, get_storage_backend, guess_content_type, normalize_storage_key
from app.services.text_extractor import extract_text as extract_binary_text

router = APIRouter(prefix="/chat", tags=["chat"])

# Supported extensions and their text extraction method
TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".csv",
    ".json",
    ".xml",
    ".yaml",
    ".yml",
    ".py",
    ".js",
    ".ts",
    ".html",
    ".css",
    ".sql",
    ".sh",
    ".log",
    ".ini",
    ".cfg",
    ".conf",
    ".env",
    ".toml",
}
OFFICE_EXTENSIONS = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt"}
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
    """Extract text from upload bytes without launching a shell or child interpreter."""
    if extension in TEXT_EXTENSIONS:
        try:
            return content.decode("utf-8", errors="replace")
        except UnicodeDecodeError:
            return content.decode("gbk", errors="replace")

    extracted = extract_binary_text(content, filename)
    if extracted:
        return extracted
    if extension == ".pdf":
        return "[PDF text extraction failed]"
    if extension == ".docx":
        return "[DOCX text extraction failed]"
    if extension in (".xlsx", ".xls"):
        return "[Excel text extraction failed]"

    return f"[Unsupported file format: {extension}]"


@router.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    agent_id: str = Form(""),
    current_user: UserRecord = Depends(get_current_user),
):
    """Upload a file for chat context. Saves to agent workspace/uploads/ and returns extracted text."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename")

    try:
        filename = validate_upload_filename(file.filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    ext = os.path.splitext(filename)[1].lower()

    content = await file.read()

    # Determine save directory
    workspace_path = ""
    if agent_id:
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
        await ensure_local_path(key)
        saved_filename = filename
    else:
        # No agent workspace exists to retain this upload. Keep its bytes request-scoped.
        file_id = str(uuid.uuid4())[:8]
        saved_filename = f"{file_id}_{filename}"

    # Extract text (only for known formats)
    is_image = ext in IMAGE_EXTENSIONS
    image_data_url = ""
    if is_image:
        # For images: generate base64 data URL for vision models
        if len(content) > 10 * 1024 * 1024:  # 10MB limit
            raise HTTPException(status_code=400, detail="Image too large (max 10MB)")
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
