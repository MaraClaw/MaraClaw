import importlib
import json
from typing import Protocol, TypeIs

from app.core.json_types import (
    JsonObject,
    JsonValue,
    is_json_object,
    is_json_value,
    json_as_str,
    json_loads_value,
    json_object_from,
    json_object_from_response,
    json_value_from_response,
)
from app.services import agent_tools
from app.services.agent_tool_exec.registry import ToolArgumentValue


class _ImageHttpResponse(Protocol):
    def raise_for_status(self) -> object: ...

    @property
    def content(self) -> bytes: ...


class _ImageHttpClient(Protocol):
    async def get(self, url: str, timeout: float = 60) -> _ImageHttpResponse: ...


def _is_image_http_client(value: object) -> TypeIs[_ImageHttpClient]:
    return callable(getattr(value, "get", None))


def _json_path_get(data: JsonValue, path: str) -> JsonValue:
    """Read a simple dotted JSON path, with numeric list indexes."""
    if not path:
        return None

    current: JsonValue = data
    for raw_part in path.split("."):
        part = raw_part.strip()
        if not part:
            continue
        if isinstance(current, list):
            if not part.isdigit():
                return None
            index = int(part)
            if index >= len(current):
                return None
            current = current[index]
        elif isinstance(current, dict):
            if part not in current:
                return None
            current = current[part]
        else:
            return None
    return current


def _render_json_template(template_json: str, variables: dict[str, str]) -> dict[str, ToolArgumentValue]:
    """Parse JSON first, then replace placeholders inside string values.

    This avoids corrupting JSON when a prompt contains quotes, newlines, or
    other characters that need escaping.
    """
    template_text = template_json.strip()
    parse_errors: list[str] = []

    candidates = [template_text]
    normalized_quotes = (
        template_text.replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'")
    )
    if normalized_quotes != template_text:
        candidates.append(normalized_quotes)

    candidates.extend(text.replace('\\"', '"') for text in tuple(candidates) if '\\"' in text)

    template: JsonValue | None = None
    for text in candidates:
        try:
            parsed_raw = json_loads_value(text)
            if isinstance(parsed_raw, str):
                parsed_raw = json_loads_value(parsed_raw)
            if not is_json_value(parsed_raw):
                parse_errors.append("Request body template is not valid JSON.")
                continue
            template = parsed_raw
            break
        except Exception as e:
            parse_errors.append(str(e))

    if template is None:
        detail = parse_errors[-1] if parse_errors else "unknown parse error"
        raise ValueError(detail)

    def render(value: JsonValue) -> JsonValue:
        if isinstance(value, str):
            rendered = value
            for key, replacement in variables.items():
                rendered = rendered.replace("{" + key + "}", replacement)
            return rendered
        if isinstance(value, list):
            return [render(item) for item in value]
        if isinstance(value, dict):
            return {key: render(item) for key, item in value.items()}
        return value

    template_value: JsonValue = template
    rendered = render(template_value)
    if not isinstance(rendered, dict):
        raise ValueError("Request body template must be a JSON object.")
    return rendered


def _json_structure_preview(data: JsonValue, depth: int = 0) -> JsonValue:
    if depth > 4:
        return "..."
    if isinstance(data, dict):
        return {k: _json_structure_preview(v, depth + 1) for k, v in list(data.items())[:12]}
    if isinstance(data, list):
        preview: list[JsonValue] = [_json_structure_preview(item, depth + 1) for item in data[:2]]
        if len(data) > 2:
            preview.append(f"... {len(data)} items total")
        return preview
    if isinstance(data, str):
        if data.startswith("data:image"):
            return f"data:image... len={len(data)}"
        if len(data) > 160:
            return data[:160] + "..."
    return data


def _find_first_image_reference(data: JsonValue) -> JsonValue:
    common_paths = [
        "choices.0.message.images.0.image_url.url",
        "choices.0.message.images.0.image_url",
        "data.0.b64_json",
        "data.0.url",
        "output.0.content.0.image_url",
        "output.0.content.0.image_base64",
    ]
    _ = importlib.import_module("app.services.agent_tools")
    for path in common_paths:
        value = agent_tools._json_path_get(data, path)
        if value:
            return value

    def walk(value: JsonValue) -> JsonValue:
        if isinstance(value, dict):
            for key in ("url", "b64_json", "image_url", "image_base64"):
                nested = value.get(key)
                if isinstance(nested, str) and nested:
                    return nested
                if isinstance(nested, dict):
                    found = walk(nested)
                    if found:
                        return found
            for nested in value.values():
                found = walk(nested)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = walk(item)
                if found:
                    return found
        elif isinstance(value, str) and value.startswith(("data:image", "http://", "https://")):
            return value
        return None

    return walk(data)


async def _custom_image_reference_to_bytes(image_ref: JsonValue, client: object) -> bytes:
    import base64

    if isinstance(image_ref, dict):
        image_ref = image_ref.get("url") or image_ref.get("b64_json") or image_ref.get("image_base64")

    if not isinstance(image_ref, str) or not image_ref:
        raise ValueError("Response image path did not resolve to a URL, data URL, or base64 string.")

    if image_ref.startswith("data:image"):
        _, _, encoded = image_ref.partition(",")
        if not encoded:
            raise ValueError("Image data URL did not contain base64 payload.")
        return base64.b64decode(encoded)

    if image_ref.startswith(("http://", "https://")):
        if not _is_image_http_client(client):
            raise TypeError("HTTP client is unavailable")
        img_resp = await client.get(image_ref, timeout=60)
        img_resp.raise_for_status()
        return img_resp.content

    return base64.b64decode(image_ref)


async def _generate_image_custom_api(
    api_key: str,
    model: str,
    base_url: str,
    endpoint_path: str,
    request_body_template_json: str,
    response_image_path: str,
    extra_headers_json: str,
    timeout_seconds: int | str,
    prompt: str,
    size: str,
) -> bytes:
    """Generate image via a configurable gateway API.

    The default request/response shape supports TokenRouter and OpenRouter:
    POST /chat/completions with image/text modalities, image returned in
    choices.0.message.images.0.image_url.url as a data URL.
    """
    import httpx

    if not base_url:
        raise ValueError("Custom image API base_url is not configured.")
    if not model:
        raise ValueError("Custom image API model is not configured.")

    timeout = int(timeout_seconds or 120)
    endpoint = endpoint_path or "/chat/completions"
    url = endpoint if endpoint.startswith(("http://", "https://")) else f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}"

    _ = importlib.import_module("app.services.agent_tools")
    variables = {"prompt": prompt, "size": size, "model": model}
    if request_body_template_json.strip():
        try:
            payload: JsonObject = agent_tools._render_json_template(request_body_template_json, variables)
        except Exception as e:
            raise ValueError(f"Invalid request_body_template_json: {e}") from e
    else:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "modalities": ["image", "text"],
            "stream": False,
        }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if extra_headers_json.strip():
        try:
            extra_raw = json_loads_value(extra_headers_json)
        except Exception as e:
            raise ValueError(f"Invalid extra_headers_json: {e}") from e
        if not is_json_object(extra_raw):
            raise ValueError("extra_headers_json must be a JSON object.")
        headers.update({str(k): str(v) for k, v in extra_raw.items() if v is not None})

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code < 200 or resp.status_code >= 300:
            try:
                err_body = json_object_from_response(resp)
                err_msg = (
                    json_as_str(json_object_from(err_body.get("error")).get("message"))
                    or json_as_str(err_body.get("message"))
                    or resp.text[:300]
                )
            except Exception:
                err_msg = resp.text[:300]
            raise ValueError(f"Custom image API error ({resp.status_code}): {err_msg}")

        try:
            data_raw = json_value_from_response(resp)
            data: JsonValue = data_raw if is_json_value(data_raw) else None
        except Exception as e:
            raise ValueError("Custom image API returned non-JSON response.") from e

        image_ref: JsonValue = agent_tools._json_path_get(data, response_image_path) if response_image_path else None
        if not image_ref:
            image_ref = agent_tools._find_first_image_reference(data)
        if not image_ref:
            preview = json.dumps(agent_tools._json_structure_preview(data), ensure_ascii=False)
            raise ValueError(
                "No image found in custom image API response. "
                + f"Check response_image_path. Response structure: {preview[:800]}"
            )

        return await agent_tools._custom_image_reference_to_bytes(image_ref, client)
