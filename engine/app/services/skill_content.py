"""Canonical stored skill content and its local identity."""

from collections.abc import Iterable, Sequence
from hashlib import sha256

from fastapi import HTTPException

from app.services.skill_provider import SkillFilePayload


def sanitized_skill_files(files: Sequence[SkillFilePayload]) -> list[SkillFilePayload]:
    paths = [file["path"] for file in files]
    if len(paths) != len(set(paths)):
        raise HTTPException(400, "Duplicate skill file paths")
    return [{"path": file["path"], "content": file["content"].replace("\x00", "")} for file in files]


def skill_fingerprint(files: Iterable[tuple[str, str]]) -> str:
    """SHA-256 of length-framed sorted paths and UTF-8 persisted text.

    This identifies local content, not provider authenticity or remote immutability.
    """
    digest = sha256(b"maraclaw-skill-files-v1\x00")
    for path, content in sorted(files):
        for value in (path, content.replace("\x00", "")):
            encoded = value.encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
    return digest.hexdigest()
