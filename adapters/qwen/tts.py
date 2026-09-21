"""Non-streaming asset TTS using the wire format from the pinned official SDK.

Offline preparation only. This client is not a realtime adapter.
"""

import http.client
import json
import os
from urllib.parse import urlsplit

from adapters.qwen.config import SDK_SOURCE

ENDPOINT = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
SOURCE = SDK_SOURCE.rsplit("/dashscope/", 1)[0] + "/dashscope/aigc/multimodal_conversation.py"


def _request(url, *, body=None, key=None, limit=32 * 1024 * 1024):
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username
        or parsed.password
        or not parsed.hostname
    ):
        raise ValueError("TTS retrieval requires an HTTP(S) URL")
    if body is not None and parsed.scheme != "https":
        raise ValueError("authenticated TTS request requires HTTPS")
    transport = (
        http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    )
    connection = transport(parsed.hostname, parsed.port, timeout=30)
    try:
        headers = {"Accept": "application/json" if body is not None else "*/*"}
        if body is not None:
            headers.update({"Content-Type": "application/json", "Authorization": "Bearer " + key})
        path = parsed.path + (("?" + parsed.query) if parsed.query else "")
        connection.request("POST" if body is not None else "GET", path, body, headers)
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError(f"TTS HTTP status {response.status}")
        data = response.read(limit + 1)
        if not data or len(data) > limit:
            raise ValueError("empty or oversized TTS response")
        return data
    except (OSError, http.client.HTTPException):
        raise RuntimeError("TTS HTTPS connection failed") from None
    finally:
        connection.close()


def synthesize(text, *, model="qwen3-tts-flash", voice="Cherry"):
    key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if not key:
        raise ValueError("DASHSCOPE_API_KEY is required")
    body = {
        "model": model,
        "input": {"text": text, "voice": voice, "language_type": "Chinese"},
        "parameters": {},
    }
    response = json.loads(
        _request(
            ENDPOINT, body=json.dumps(body, ensure_ascii=False).encode(), key=key, limit=1024 * 1024
        )
    )
    try:
        url = response["output"]["audio"]["url"]
    except (TypeError, KeyError):
        raise RuntimeError("TTS response did not contain an audio URL") from None
    # Fetch the signed output without forwarding API credentials; never persist this URL.
    audio = _request(url)
    return audio, {
        "request_id": response.get("request_id"),
        "sdk_version": None,
        "transport": "stdlib.https",
        "protocol_source": SOURCE,
    }
