import importlib
import uuid
from pathlib import Path

import anyio

from app.core.json_types import json_as_int, json_as_str_or, json_object_from_response
from app.core.logging import logger
from app.services import agent_tools

from .registry import ToolArguments, ToolArgumentValue


def _string_value(value: ToolArgumentValue | None, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _integer_or_string_value(value: object, default: int) -> int | str:
    if isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool)):
        return value
    return default


async def _upload_image(agent_id: uuid.UUID, ws: Path, arguments: ToolArguments) -> str:
    """Upload an image to ImageKit CDN and return the public URL.

    Credential resolution order:
    1. Global tool config (admin-set, shared by all agents)
    2. Per-agent tool config override (agent-specific)
    """
    import base64

    import httpx

    file_path = _string_value(arguments.get("file_path"))
    url = _string_value(arguments.get("url"))
    file_name = _string_value(arguments.get("file_name"))
    folder = _string_value(arguments.get("folder"), "/maraclaw")

    if not file_path and not url:
        return "❌ Please provide either 'file_path' (workspace path) or 'url' (public image URL)"

    _ = importlib.import_module("app.services.agent_tools")
    private_key = ""
    try:
        config = await agent_tools._get_tool_config(agent_id, "upload_image") or {}
        private_key = json_as_str_or(config.get("private_key"))
    except Exception as e:
        logger.error(f"[UploadImage] Config load error: {e}")

    if not private_key:
        return "❌ ImageKit Private Key not configured. Ask your admin to configure it in Enterprise Settings → Tools → Upload Image, or set it in your agent's tool config."

    form_data = {}
    file_content = None

    if file_path:
        full_path = (ws / file_path).resolve()
        if not str(full_path).startswith(str(ws)):
            return "❌ Access denied: path is outside the workspace"
        if not full_path.exists():
            return f"❌ File not found: {file_path}"
        if not full_path.is_file():
            return f"❌ Not a file: {file_path}"

        size_mb = full_path.stat().st_size / (1024 * 1024)
        if size_mb > 25:
            return f"❌ File too large ({size_mb:.1f}MB). Maximum is 25MB."

        file_content = full_path.read_bytes()
        if not file_name:
            file_name = full_path.name
    elif url:
        form_data["file"] = url
        if not file_name:
            from urllib.parse import urlparse

            file_name = urlparse(url).path.split("/")[-1] or "image.jpg"

    if not file_name:
        file_name = "image.png"

    form_data["fileName"] = file_name
    form_data["folder"] = folder
    form_data["useUniqueFileName"] = "true"

    auth_string = base64.b64encode(f"{private_key}:".encode()).decode()

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            if file_content:
                files = {"file": (file_name, file_content)}
                resp = await client.post(
                    "https://upload.imagekit.io/api/v2/files/upload",
                    headers={"Authorization": f"Basic {auth_string}"},
                    data=form_data,
                    files=files,
                )
            else:
                resp = await client.post(
                    "https://upload.imagekit.io/api/v2/files/upload",
                    headers={"Authorization": f"Basic {auth_string}"},
                    data=form_data,
                )

        if resp.status_code in (200, 201):
            result = json_object_from_response(resp)
            cdn_url = json_as_str_or(result.get("url"))
            file_id = json_as_str_or(result.get("fileId"))
            size = json_as_int(result.get("size"))
            size_str = f"{size / 1024:.1f}KB" if size < 1024 * 1024 else f"{size / (1024 * 1024):.1f}MB"
            return (
                "✅ Image uploaded successfully!\n\n"
                + f"**CDN URL**: {cdn_url}\n"
                + f"**File ID**: {file_id}\n"
                + f"**Size**: {size_str}\n"
                + f"**Name**: {json_as_str_or(result.get('name'), file_name)}"
            )
        error_detail = resp.text[:300]
        return f"❌ Upload failed (HTTP {resp.status_code}): {error_detail}"

    except httpx.TimeoutException:
        return "❌ Upload timed out after 60s. The file may be too large or the network is slow."
    except Exception as e:
        return f"❌ Upload error: {type(e).__name__}: {str(e)[:300]}"


async def _generate_image(agent_id: uuid.UUID, ws: Path, arguments: ToolArguments, provider: str) -> str:
    """Generate an image using the configured provider and save to workspace.

    Supported providers:
    - siliconflow: OpenAI-compatible API (FLUX models, China-friendly)
    - openai: Native OpenAI API (GPT Image)
    - google: Google Gemini Native Image API (Nano Banana)
    - grok: xAI Grok Imagine (OpenAI-compatible images.generate)
    - custom: Configurable HTTP API for gateways such as TokenRouter/OpenRouter

    The tool config is resolved via the standard _get_tool_config() hierarchy:
    global tool config (admin-set) -> per-agent tool config override.
    """
    from datetime import UTC, datetime

    import httpx

    prompt = _string_value(arguments.get("prompt"))
    if not prompt:
        return "❌ Missing required argument 'prompt' for generate_image"

    size = _string_value(arguments.get("size"), "1024x1024")
    save_path = _string_value(arguments.get("save_path"))

    _ = importlib.import_module("app.services.agent_tools")
    tool_key = f"generate_image_{provider}"
    config = await agent_tools._get_tool_config(agent_id, tool_key) or {}
    model = json_as_str_or(config.get("model"))
    api_key = json_as_str_or(config.get("api_key"))
    base_url = json_as_str_or(config.get("base_url"))
    if provider == "grok":
        from app.services.agent_tool_exec.xai_credentials import (
            missing_xai_key_message,
            resolve_xai_api_key,
            resolve_xai_base_url,
        )

        api_key = resolve_xai_api_key(api_key)
        base_url, base_error = resolve_xai_base_url(base_url)
        if base_error:
            return f"❌ {base_error}"
        if not api_key:
            return missing_xai_key_message("Grok Imagine")
        prompt = prompt[:4000]
        model = model or "grok-imagine-image-2.0"

    if not api_key:
        return (
            "❌ Image generation API key not configured. "
            + "Ask your admin to configure it in Enterprise Settings → Tools → Generate Image."
        )

    if not save_path:
        ts = datetime.now(UTC).astimezone().strftime("%Y%m%d_%H%M%S")
        slug = "_".join(prompt.split()[:4]).lower()
        slug = "".join(c for c in slug if c.isalnum() or c == "_")[:40]
        save_path = f"workspace/images/{slug}_{ts}.png"

    full_save_path = Path(await anyio.Path(ws / save_path).resolve())
    workspace_root = Path(await anyio.Path(ws).resolve())
    if not full_save_path.is_relative_to(workspace_root):
        return "❌ Access denied: save path is outside the workspace"
    await anyio.Path(full_save_path.parent).mkdir(parents=True, exist_ok=True)

    try:
        if provider == "siliconflow":
            image_bytes = await agent_tools._generate_image_siliconflow(
                api_key,
                model or "black-forest-labs/FLUX.1-schnell",
                base_url or "https://api.siliconflow.cn/v1",
                prompt,
                size,
            )
        elif provider == "openai":
            image_bytes = await agent_tools._generate_image_openai(
                api_key,
                model or "gpt-image-1",
                base_url or "https://api.openai.com/v1",
                prompt,
                size,
            )
        elif provider == "google":
            image_bytes = await agent_tools._generate_image_google(
                api_key,
                model or "gemini-2.5-flash-image",
                base_url or "https://generativelanguage.googleapis.com/v1beta",
                prompt,
                size,
            )
        elif provider == "grok":
            image_bytes = await agent_tools._generate_image_grok(
                api_key,
                model,
                base_url,
                prompt,
                size,
            )
        elif provider == "custom":
            image_bytes = await agent_tools._generate_image_custom_api(
                api_key=api_key,
                model=model,
                base_url=base_url,
                endpoint_path=json_as_str_or(config.get("endpoint_path"), "/chat/completions"),
                request_body_template_json=json_as_str_or(config.get("request_body_template_json")),
                response_image_path=json_as_str_or(
                    config.get("response_image_path"), "choices.0.message.images.0.image_url.url"
                ),
                extra_headers_json=json_as_str_or(config.get("extra_headers_json")),
                timeout_seconds=_integer_or_string_value(config.get("timeout_seconds"), 120),
                prompt=prompt,
                size=size,
            )
        else:
            return f"❌ Unknown image generation provider: {provider}. Supported: siliconflow, openai, google, grok, custom"

        if not image_bytes:
            return "❌ Image generation returned empty result. Please try a different prompt."

        _ = await anyio.Path(full_save_path).write_bytes(image_bytes)
        size_kb = len(image_bytes) / 1024
        api_image_path = f"/api/agents/{agent_id}/files/download?path={save_path}"

        return (
            f"✅ Image generated and saved to: {save_path}\n"
            + f"Size: {size_kb:.1f} KB | Provider: {provider} | Model: {model or '(default)'}\n\n"
            + "Display this image to the user using this exact markdown:\n"
            + f"![generated image]({api_image_path})"
        )
    except httpx.TimeoutException:
        logger.error(f"[GenerateImage] Timeout ({provider}): took longer than 120 seconds or network unreachable.")
        return (
            f"❌ Image generation failed ({provider}): API request timed out after 120 seconds. "
            + "This is usually caused by network issues or the model taking too long to generate."
        )
    except Exception as e:
        err_msg = str(e) or type(e).__name__
        logger.error(f"[GenerateImage] Error ({provider}): {err_msg}")
        return f"❌ Image generation failed ({provider}): {err_msg[:400]}"
