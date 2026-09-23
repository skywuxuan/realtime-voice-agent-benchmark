import json

import pytest

from adapters.qwen import tts


def test_asset_tts_wire_payload_and_signed_download_never_forward_key(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "fake-asset-key")
    requests = []

    def request(url, **kwargs):
        requests.append((url, kwargs))
        if len(requests) == 1:
            return json.dumps(
                {
                    "request_id": "request_fixture",
                    "output": {"audio": {"url": "https://fixture.invalid/audio?signature=private"}},
                }
            ).encode()
        return b"frozen-audio"

    monkeypatch.setattr(tts, "_request", request)
    pcm, meta = tts.synthesize("嗯嗯")
    assert pcm == b"frozen-audio"
    assert requests[0][0] == tts.ENDPOINT
    assert requests[0][1]["key"] == "fake-asset-key"
    assert json.loads(requests[0][1]["body"]) == {
        "model": "qwen3-tts-flash",
        "input": {"text": "嗯嗯", "voice": "Cherry", "language_type": "Chinese"},
        "parameters": {},
    }
    assert requests[1][1] == {}
    assert "private" not in json.dumps(meta) and "fake-asset-key" not in json.dumps(meta)


def test_asset_tts_retries_rate_limits_without_retrying_other_client_errors(monkeypatch):
    responses = iter(((429, "0.25", b"limited"), (200, None, b"audio")))
    sleeps = []
    monkeypatch.setattr(tts, "_request_once", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(tts.time, "sleep", sleeps.append)
    assert tts._request("https://example.test/audio", retry_delays=(5,)) == b"audio"
    assert sleeps == [0.25]

    monkeypatch.setattr(tts, "_request_once", lambda *args, **kwargs: (400, None, b"bad"))
    with pytest.raises(RuntimeError, match="TTS HTTP status 400"):
        tts._request("https://example.test/audio", retry_delays=(0,))


def test_asset_tts_retries_download_connection_without_resubmitting_generation(monkeypatch):
    calls = []
    sleeps = []

    def request(url, **kwargs):
        calls.append(kwargs["body"])
        if len(calls) == 1:
            raise RuntimeError("TTS HTTPS connection failed")
        return 200, None, b"audio"

    monkeypatch.setattr(tts, "_request_once", request)
    monkeypatch.setattr(tts.time, "sleep", sleeps.append)
    assert tts._request("https://example.test/audio", retry_delays=(1,)) == b"audio"
    assert calls == [None, None] and sleeps == [1]
    calls.clear()
    with pytest.raises(RuntimeError, match="TTS HTTPS connection failed"):
        tts._request("https://example.test/generate", body=b"{}", key="secret")
    assert calls == [b"{}"]
