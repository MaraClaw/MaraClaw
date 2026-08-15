from typing import Protocol

from app.core.json_types import (
    JsonObject,
    json_as_str,
    json_as_str_or,
    json_object_from,
    object_list_from_row,
)


class _JsonResponse(Protocol):
    status_code: int

    @property
    def text(self) -> str: ...

    def json(self) -> object: ...


def _response_mapping(response: _JsonResponse) -> JsonObject:
    raw: object = response.json()
    return json_object_from(raw)


def _first_image(data: JsonObject) -> JsonObject:
    images = object_list_from_row(data.get("data", [{}]))
    return json_object_from(images[0])


def _api_error_message(response: _JsonResponse) -> str:
    err_body = _response_mapping(response)
    nested = json_object_from(err_body.get("error"))
    return json_as_str(err_body.get("message")) or json_as_str(nested.get("message")) or response.text[:300]


async def _generate_image_siliconflow(api_key: str, model: str, base_url: str, prompt: str, size: str) -> bytes:
    """Generate image via SiliconFlow (OpenAI-compatible images.generate API).

    SiliconFlow returns a temporary URL (expires in ~1 hour), so we download
    the image bytes immediately after generation.
    """
    import base64

    import httpx

    url = f"{base_url.rstrip('/')}/images/generations"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload: JsonObject = {
        "model": model,
        "prompt": prompt,
        "image_size": size,
        "n": 1,
    }

    async with httpx.AsyncClient(timeout=120) as client:
        resp: httpx.Response = await client.post(url, json=payload, headers=headers)
        if resp.status_code != 200:
            try:
                err_msg = _api_error_message(resp)
            except Exception:
                err_msg = resp.text[:300]
            raise ValueError(f"SiliconFlow API error ({resp.status_code}): {err_msg}")
        data = _response_mapping(resp)

        image_data = _first_image(data)
        image_url = json_as_str(image_data.get("url"))
        if image_url:
            img_resp: httpx.Response = await client.get(image_url, timeout=60)
            _ = img_resp.raise_for_status()
            return img_resp.content

        b64 = json_as_str(image_data.get("b64_json"))
        if b64:
            return base64.b64decode(b64)

        raise ValueError(f"No image URL or b64_json in SiliconFlow response: {data}")


async def _generate_image_openai(api_key: str, model: str, base_url: str, prompt: str, size: str) -> bytes:
    """Generate image via OpenAI GPT Image API.

    Requests b64_json format to avoid dealing with URL expiry.
    """
    import base64

    import httpx

    url = f"{base_url.rstrip('/')}/images/generations"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload: JsonObject = {
        "model": model,
        "prompt": prompt,
        "size": size,
        "n": 1,
        "response_format": "b64_json",
    }

    async with httpx.AsyncClient(timeout=120) as client:
        resp: httpx.Response = await client.post(url, json=payload, headers=headers)
        if resp.status_code != 200:
            try:
                err_msg = _api_error_message(resp)
            except Exception:
                err_msg = resp.text[:300]
            raise ValueError(f"OpenAI API error ({resp.status_code}): {err_msg}")
        data = _response_mapping(resp)

        image_data = _first_image(data)
        b64 = json_as_str(image_data.get("b64_json"))
        if b64:
            return base64.b64decode(b64)

        image_url = json_as_str(image_data.get("url"))
        if image_url:
            img_resp: httpx.Response = await client.get(image_url, timeout=60)
            _ = img_resp.raise_for_status()
            return img_resp.content

        raise ValueError(f"No b64_json or URL in OpenAI response: {data}")


async def _generate_image_google(api_key: str, model: str, base_url: str, prompt: str, size: str) -> bytes:
    """Generate image via Google Gemini Native Image API (Nano Banana) or Vertex AI.

    Uses the Gemini generateContent endpoint with responseModalities=["IMAGE"].
    Converts WxH size to aspect ratio format (e.g. 1024x1024 -> 1:1).
    Extracts the generated image from inlineData in the response parts.
    """
    import base64

    import httpx

    url = f"{base_url.rstrip('/')}/models/{model}:generateContent"
    size_to_ratio = {
        "1024x1024": "1:1",
        "768x1024": "3:4",
        "1024x768": "4:3",
        "768x1366": "9:16",
        "1366x768": "16:9",
        "1024x1536": "3:4",
        "1536x1024": "4:3",
    }
    aspect_ratio = size_to_ratio.get(size, "1:1")

    payload: JsonObject = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseModalities": ["IMAGE"],
            "imageConfig": {
                "aspectRatio": aspect_ratio,
            },
        },
    }

    async with httpx.AsyncClient(timeout=120) as client:
        resp: httpx.Response = await client.post(
            url,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": api_key,
            },
        )
        if resp.status_code != 200:
            try:
                err_msg = _api_error_message(resp)
            except Exception:
                err_msg = resp.text[:300]
            raise ValueError(f"Google Gemini API error ({resp.status_code}): {err_msg}")
        data = _response_mapping(resp)

        candidates = object_list_from_row(data.get("candidates"))
        if not candidates:
            raise ValueError(f"No candidates in Gemini response: {data}")

        parts = object_list_from_row(json_object_from(json_object_from(candidates[0]).get("content")).get("parts"))
        part_summaries: list[str] = []
        for part in parts:
            part_obj = json_object_from(part)
            if "text" in part_obj:
                part_summaries.append(json_as_str_or(part_obj.get("text"), "(image)"))
            else:
                part_summaries.append("(inline)")
            if "inlineData" in part_obj:
                b64 = json_as_str(json_object_from(part_obj.get("inlineData")).get("data"))
                if b64:
                    return base64.b64decode(b64)

        raise ValueError("No image (inlineData) found in Gemini response parts. " + f"Parts: {part_summaries}")


_KNOWN_ASPECT_RATIOS: tuple[tuple[str, float], ...] = (
    ("1:1", 1.0),
    ("16:9", 16 / 9),
    ("9:16", 9 / 16),
    ("4:3", 4 / 3),
    ("3:4", 3 / 4),
    ("3:2", 1.5),
    ("2:3", 2 / 3),
    ("2:1", 2.0),
    ("1:2", 0.5),
    ("19.5:9", 19.5 / 9),
    ("9:19.5", 9 / 19.5),
    ("20:9", 20 / 9),
    ("9:20", 9 / 20),
)

_EXACT_SIZE_RATIOS = {
    "1024x1024": "1:1",
    "768x1024": "3:4",
    "1024x768": "4:3",
    "768x1366": "9:16",
    "1366x768": "16:9",
    "1024x1536": "2:3",
    "1536x1024": "3:2",
    "1152x768": "3:2",
    "768x1152": "2:3",
    "1536x768": "2:1",
    "768x1536": "1:2",
    "1920x1080": "16:9",
    "1080x1920": "9:16",
    "2048x1024": "2:1",
    "1024x2048": "1:2",
}


def _normalize_size_token(size: str) -> str:
    return size.strip().lower().replace(" ", "")


def _parse_width_height(token: str) -> tuple[float, float] | None:
    separator = "x" if "x" in token and ":" not in token else ":" if ":" in token else ""
    if not separator:
        return None
    left, _, right = token.partition(separator)
    try:
        width = float(left)
        height = float(right)
    except ValueError:
        return None
    if width <= 0 or height <= 0:
        return None
    return width, height


def _size_to_aspect_ratio(size: str) -> str:
    normalized = _normalize_size_token(size)
    if normalized == "auto":
        return "auto"
    if normalized in _EXACT_SIZE_RATIOS:
        return _EXACT_SIZE_RATIOS[normalized]
    for label, _ratio in _KNOWN_ASPECT_RATIOS:
        if label.lower() == normalized:
            return label
    parsed = _parse_width_height(normalized)
    if parsed is None:
        return "1:1"
    width, height = parsed
    target = width / height
    return min(_KNOWN_ASPECT_RATIOS, key=lambda item: abs(item[1] - target))[0]


def _size_to_resolution(size: str) -> str | None:
    normalized = _normalize_size_token(size)
    if "x" not in normalized or ":" in normalized:
        return None
    parsed = _parse_width_height(normalized)
    if parsed is None:
        return None
    return "2k" if max(parsed) >= 1536 else "1k"


async def _generate_image_grok(api_key: str, model: str, base_url: str, prompt: str, size: str) -> bytes:
    """Generate image via xAI Grok Imagine (OpenAI-compatible images.generate).

    Grok Imagine uses aspect_ratio rather than WxH size. WxH values are mapped
    to the closest supported ratio. Requests b64_json to avoid URL expiry.
    """
    import base64

    import httpx

    url = f"{base_url.rstrip('/')}/images/generations"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload: JsonObject = {
        "model": model,
        "prompt": prompt,
        "n": 1,
        "response_format": "b64_json",
        "aspect_ratio": _size_to_aspect_ratio(size),
    }
    resolution = _size_to_resolution(size)
    if resolution:
        payload["resolution"] = resolution

    async with httpx.AsyncClient(timeout=120) as client:
        resp: httpx.Response = await client.post(url, json=payload, headers=headers)
        if resp.status_code != 200:
            try:
                err_msg = _api_error_message(resp)
            except Exception:
                err_msg = resp.text[:300]
            raise ValueError(f"Grok Imagine API error ({resp.status_code}): {err_msg}")
        data = _response_mapping(resp)

        image_data = _first_image(data)
        b64 = json_as_str(image_data.get("b64_json"))
        if b64:
            return base64.b64decode(b64)

        image_url = json_as_str(image_data.get("url"))
        if image_url:
            img_resp: httpx.Response = await client.get(image_url, timeout=60)
            _ = img_resp.raise_for_status()
            return img_resp.content

        raise ValueError(f"No b64_json or URL in Grok Imagine response: {data}")
