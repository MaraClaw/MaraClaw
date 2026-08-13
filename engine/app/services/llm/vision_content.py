"""Conversion of screenshot vision content into LLM content parts."""

from typing import assert_never

from app.services.vision_inject import VisionContent

from .types import LLMContentPart


def rebuild_llm_content_parts(content: list[VisionContent]) -> list[LLMContentPart]:
    """Rebuild provider-neutral parts without retaining mutable input mappings."""
    parts: list[LLMContentPart] = []
    for vision_part in content:
        match vision_part["type"]:
            case "text":
                parts.append(LLMContentPart(type="text", text=vision_part["text"]))
            case "image_url":
                parts.append(
                    LLMContentPart(
                        type="image_url",
                        image_url={"url": vision_part["image_url"]["url"]},
                    )
                )
            case unexpected:
                assert_never(unexpected)
    return parts
