async def _generate_image_siliconflow(api_key: str, model: str, base_url: str, prompt: str, size: str) -> bytes:
    """Generate image via SiliconFlow (OpenAI-compatible images.generate API).

    SiliconFlow returns a temporary URL (expires in ~1 hour), so we download
    the image bytes immediately after generation.
    """
    import base64

    import httpx

    url = f"{base_url.rstrip('/')}/images/generations"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "prompt": prompt,
        "image_size": size,
        "n": 1,
    }

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code != 200:
            try:
                err_body = resp.json()
                err_msg = err_body.get("message") or err_body.get("error", {}).get("message", resp.text[:300])
            except Exception:
                err_msg = resp.text[:300]
            raise ValueError(f"SiliconFlow API error ({resp.status_code}): {err_msg}")
        data = resp.json()

        image_data = data.get("data", [{}])[0]
        image_url = image_data.get("url")
        if image_url:
            img_resp = await client.get(image_url, timeout=60)
            img_resp.raise_for_status()
            return img_resp.content

        b64 = image_data.get("b64_json")
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
    payload = {
        "model": model,
        "prompt": prompt,
        "size": size,
        "n": 1,
        "response_format": "b64_json",
    }

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code != 200:
            try:
                err_body = resp.json()
                err_msg = err_body.get("error", {}).get("message", resp.text[:300])
            except Exception:
                err_msg = resp.text[:300]
            raise ValueError(f"OpenAI API error ({resp.status_code}): {err_msg}")
        data = resp.json()

        image_data = data.get("data", [{}])[0]
        b64 = image_data.get("b64_json")
        if b64:
            return base64.b64decode(b64)

        image_url = image_data.get("url")
        if image_url:
            img_resp = await client.get(image_url, timeout=60)
            img_resp.raise_for_status()
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

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseModalities": ["IMAGE"],
            "imageConfig": {
                "aspectRatio": aspect_ratio,
            },
        },
    }

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            url,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": api_key,
            },
        )
        if resp.status_code != 200:
            try:
                err_body = resp.json()
                err_msg = err_body.get("error", {}).get("message", resp.text[:300])
            except Exception:
                err_msg = resp.text[:300]
            raise ValueError(f"Google Gemini API error ({resp.status_code}): {err_msg}")
        data = resp.json()

        candidates = data.get("candidates", [])
        if not candidates:
            raise ValueError(f"No candidates in Gemini response: {data}")

        parts = candidates[0].get("content", {}).get("parts", [])
        for part in parts:
            if "inlineData" in part:
                b64 = part["inlineData"]["data"]
                return base64.b64decode(b64)

        raise ValueError(
            f"No image (inlineData) found in Gemini response parts. "
            f"Parts: {[p.get('text', '(image)') if 'text' in p else '(inline)' for p in parts]}"
        )
