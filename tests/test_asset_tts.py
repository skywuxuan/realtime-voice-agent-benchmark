import json

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
