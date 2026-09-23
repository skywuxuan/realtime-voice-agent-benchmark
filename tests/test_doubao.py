import asyncio
import base64
import json

import pytest

from adapters.base import SessionConfig, ToolDefinition
from adapters.doubao import DoubaoRealtimeAdapter, DoubaoSettings
from adapters.doubao.config import APP_KEY, ENDPOINT, INPUT_FORMAT, OUTPUT_FORMAT, RESOURCE_ID
from benchmark.audio import AudioFrame
from events.recorder import EventRecorder
from events.schema import ToolResult

APP_ID = "fixture-app"
ACCESS_KEY = "fixture-token"


def seed_config():
    return SessionConfig(
        model="seed-duplex-3.0",
        voice="zh_female_vv_jupiter_bigtts",
        turn_mode="server_vad",
        input_audio=INPUT_FORMAT,
        output_audio=OUTPUT_FORMAT,
        system_prompt="座舱测试",
        tools=(
            ToolDefinition(
                name="setVolume",
                description="设置音量",
                parameters={
                    "type": "object",
                    "properties": {"volume": {"type": "integer"}},
                    "additionalProperties": False,
                },
            ),
        ),
    )


class SeedSocket:
    def __init__(self, *, auto_response=True):
        self.queue = asyncio.Queue()
        self.sent = []
        self.close_code = None
        self.serial = 0
        self.responded = False
        self.auto_response = auto_response

    def push(self, data):
        self.serial += 1
        self.queue.put_nowait(json.dumps({"event_id": f"seed_{self.serial}", **data}))

    async def recv(self):
        value = await self.queue.get()
        if value is None:
            raise EOFError("closed")
        return value

    async def send(self, text):
        data = json.loads(text)
        self.sent.append(data)
        if data["type"] == "session.create":
            self.push({"type": "session.created", "session": {"id": "seed-session"}})
        elif (
            data["type"] == "input_audio_buffer.append"
            and self.auto_response
            and not self.responded
        ):
            self.responded = True
            self.push(
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "item_id": "user_1",
                    "transcript": "把音量调到四十",
                }
            )
            self.push(
                {
                    "type": "response.function_call_arguments.done",
                    "items": [
                        {
                            "call_id": "call_1",
                            "name": "setVolume",
                            "arguments": '{"volume":40}',
                        }
                    ],
                }
            )
            self.push({"type": "response.done", "usage": {"total_tokens": 12}})
        elif data["type"] == "conversation.item.create":
            self.push({"type": "response.output_text.delta", "delta": "已"})
            self.push({"type": "response.output_text.done", "text": "已调到四十"})
            audio = base64.b64encode(b"\x01\x00" * 480).decode()
            self.push({"type": "response.output_audio.started"})
            self.push({"type": "response.output_audio.delta", "audio": audio})
            self.push({"type": "response.output_audio.done"})
            self.push({"type": "response.done", "usage": {"total_tokens": 8}})
        elif data["type"] == "session.close":
            self.push({"type": "session.closed"})

    async def close(self):
        self.close_code = 1000
        self.queue.put_nowait(None)


@pytest.fixture
def seed_credentials(monkeypatch):
    monkeypatch.setenv("BYTEDANCE_LLM_APPID", APP_ID)
    monkeypatch.setenv("BYTEDANCE_LLM_TOKEN", ACCESS_KEY)


def factory_for(ws):
    async def connect(url, **kwargs):
        assert url == ENDPOINT
        headers = kwargs["additional_headers"]
        assert headers["X-Api-App-Id"] == APP_ID
        assert headers["X-Api-Access-Key"] == ACCESS_KEY
        assert headers["X-Api-Resource-Id"] == RESOURCE_ID
        assert headers["X-Api-App-Key"] == APP_KEY
        assert headers["X-Api-Request-Id"]
        return ws

    return connect


def test_seed_duplex_requires_both_environment_credentials(monkeypatch, context, clock):
    monkeypatch.delenv("BYTEDANCE_LLM_API_KEY", raising=False)
    monkeypatch.delenv("BYTEDANCE_LLM_APPID", raising=False)
    monkeypatch.delenv("BYTEDANCE_LLM_TOKEN", raising=False)
    with pytest.raises(ValueError, match="BYTEDANCE_LLM_API_KEY"):
        DoubaoRealtimeAdapter(context, object(), clock=clock)


def test_seed_duplex_prefers_new_api_key(monkeypatch, context, clock):
    monkeypatch.setenv("BYTEDANCE_LLM_API_KEY", "fixture-new-api-key")
    monkeypatch.setenv("BYTEDANCE_LLM_APPID", APP_ID)
    monkeypatch.setenv("BYTEDANCE_LLM_TOKEN", ACCESS_KEY)
    adapter = DoubaoRealtimeAdapter(context, object(), clock=clock)
    assert adapter._api_key == "fixture-new-api-key"


def test_seed_duplex_official_session_and_tool_roundtrip(
    tmp_path, context, clock, seed_credentials
):
    async def run():
        ws = SeedSocket()
        async with EventRecorder(
            tmp_path / "case", context, clock=clock, secrets=(APP_ID, ACCESS_KEY)
        ) as recorder:
            adapter = DoubaoRealtimeAdapter(
                context,
                recorder,
                clock=clock,
                connector=factory_for(ws),
                settings=DoubaoSettings(request_timeout_s=1, close_timeout_s=0.2),
            )
            info = await adapter.connect()
            assert info.vendor_session_id is None
            await adapter.configure(seed_config())
            await adapter.send_audio(
                AudioFrame(
                    pcm=b"\0\0" * 320,
                    format=INPUT_FORMAT,
                    stream_id="input",
                    turn_id="t1",
                    chunk_index=0,
                    sample_offset=0,
                )
            )
            seen = []
            while True:
                event = await adapter.receive_event()
                seen.append(event)
                await recorder.record(event)
                if event.event == "tool_call_end":
                    break
            await adapter.send_tool_result(
                ToolResult(
                    call_id="call_1",
                    status="success",
                    result={"acknowledged": True},
                    execution_id="exec_1",
                )
            )
            while not any(event.event == "assistant_audio_chunk" for event in seen):
                event = await adapter.receive_event()
                seen.append(event)
                await recorder.record(event)
            await adapter.close()
            while True:
                try:
                    await recorder.record(await adapter.receive_event())
                except EOFError:
                    break
        create = next(item for item in ws.sent if item["type"] == "session.create")
        assert create["session"]["model"] == "1.2.6.1"
        assert create["session"]["audio"]["input"]["format"] == {
            "type": "pcm",
            "sample_rate": 16000,
        }
        assert create["session"]["tools"][0]["name"] == "setVolume"
        result = next(item for item in ws.sent if item["type"] == "conversation.item.create")
        mute_index = next(
            index for index, item in enumerate(ws.sent) if item["type"] == "input_audio_mute.commit"
        )
        assert mute_index < ws.sent.index(result)
        assert result["items"][0]["role"] == "tool"
        assert result["items"][0]["call_id"] == "call_1"
        assert json.loads(result["items"][0]["content"][0]["text"])["acknowledged"]
        assert ws.sent[-1]["type"] == "session.close"
        assert any(event.event == "assistant_response_start" for event in seen)
        assert any(event.event == "assistant_audio_start" for event in seen)
        assert all(
            event.turn_id == "t1"
            for event in seen
            if event.event in {"assistant_response_start", "tool_call_end"}
        )
        assert all(event.producer == "adapter.doubao" for event in seen)
        for path in (tmp_path / "case").glob("*.json*"):
            assert APP_ID not in path.read_text()
            assert ACCESS_KEY not in path.read_text()

    asyncio.run(run())


def test_seed_duplex_mutes_idle_audio_and_unmutes_next_input(
    tmp_path, context, clock, seed_credentials
):
    async def run():
        ws = SeedSocket(auto_response=False)
        async with EventRecorder(
            tmp_path / "case", context, clock=clock, secrets=(APP_ID, ACCESS_KEY)
        ) as recorder:
            adapter = DoubaoRealtimeAdapter(
                context,
                recorder,
                clock=clock,
                connector=factory_for(ws),
                settings=DoubaoSettings(
                    request_timeout_s=1,
                    close_timeout_s=0.2,
                    input_idle_mute_s=0.05,
                ),
            )
            await adapter.connect()
            await adapter.configure(seed_config())
            first = AudioFrame(
                pcm=b"\0\0" * 320,
                format=INPUT_FORMAT,
                stream_id="input",
                turn_id="t1",
                chunk_index=0,
                sample_offset=0,
            )
            await adapter.send_audio(first)
            await asyncio.sleep(0.08)
            assert [item["type"] for item in ws.sent][-1] == "input_audio_mute.commit"
            await adapter.send_audio(
                first.model_copy(update={"turn_id": "t2", "chunk_index": 1})
            )
            types = [item["type"] for item in ws.sent]
            assert types[-2:] == ["input_audio_unmute.commit", "input_audio_buffer.append"]
            await adapter.close()
            while True:
                try:
                    await recorder.record(await adapter.receive_event())
                except EOFError:
                    break

    asyncio.run(run())
