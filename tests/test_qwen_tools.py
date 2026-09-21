"""Wire-shape tests based on the pinned official Qwen function-call example."""

import asyncio
import base64
import json

import pytest
from test_qwen import FakeSocket, factory_for, session_config

from adapters.base import CapabilityManifest
from adapters.qwen.protocol import QwenEventMapper
from benchmark.audio import AudioFrame, AudioRef
from events.clock import SystemClock
from events.schema import ToolResult
from tools.definitions import definitions


class MemoryArtifacts:
    def __init__(self):
        self.raw = []
        self.audio = {}

    async def record_raw(self, event):
        self.raw.append(event)
        return event

    async def store_audio(self, stream_id, pcm, format):
        offset = len(self.audio.get(stream_id, b""))
        self.audio[stream_id] = self.audio.get(stream_id, b"") + pcm
        return AudioRef(
            **format.model_dump(),
            path="audio/fixture.pcm",
            byte_offset=offset,
            byte_length=len(pcm),
            sample_offset=offset // 2,
            sample_count=len(pcm) // 2,
        )


class ToolSocket(FakeSocket):
    def __init__(self, mode="tools"):
        super().__init__(auto_response=False)
        self.mode, self.responses = mode, 0

    async def send(self, text):
        await super().send(text)
        data = json.loads(text)
        if data["type"] != "response.create":
            return
        self.responses += 1
        rid = f"r{self.responses}"
        self.push({"type": "response.created", "response": {"id": rid, "status": "in_progress"}})
        if (self.responses == 1 and self.mode != "no_calls") or (
            self.mode == "retry" and self.responses == 2
        ):
            for index in range(1 if self.mode == "retry" else 2):
                self.push(
                    {
                        "type": "response.function_call_arguments.done",
                        "response_id": rid,
                        "call_id": f"c{index}"
                        if self.mode != "retry"
                        else f"retry{self.responses}",
                        "name": "weather",
                        "arguments": json.dumps(
                            {"city": "上海" if index == 0 else "北京", "date": "2026-09-20"}
                        ),
                    }
                )
        else:
            self.push(
                {
                    "type": "response.audio.delta",
                    "response_id": rid,
                    "delta": base64.b64encode(b"\1\0" * 480).decode(),
                }
            )
            self.push(
                {
                    "type": "response.audio_transcript.done",
                    "response_id": rid,
                    "transcript": "查询结果已返回。",
                }
            )
            self.push({"type": "response.audio.done", "response_id": rid})
        self.push({"type": "response.done", "response": {"id": rid, "status": "completed"}})


def test_tool_batch_is_injected_once_then_response_resumes(context, monkeypatch):
    from test_qwen import FAKE_SECRET

    monkeypatch.setenv("DASHSCOPE_API_KEY", FAKE_SECRET)

    async def run():
        ws, sink, clock = ToolSocket(), MemoryArtifacts(), SystemClock()
        adapter = factory_for(ws)(context, sink, clock)
        await adapter.connect()
        config = session_config().model_copy(update={"tools": definitions(("weather",))})
        await adapter.configure(config)
        assert ws.session["tools"][0]["function"]["name"] == "weather"
        await adapter.send_audio(
            AudioFrame(
                pcm=b"\1\0" * 320,
                format=config.input_audio,
                stream_id="input",
                turn_id="t1",
                chunk_index=0,
                sample_offset=0,
            )
        )
        await adapter.commit_turn("t1")
        events = []
        while True:
            e = await adapter.receive_event()
            events.append(e)
            if e.event == "assistant_response_end":
                break
        calls = [e for e in events if e.event == "tool_call_end"]
        assert len(calls) == 2
        for i, call in enumerate(calls):
            result = ToolResult(
                call_id=call.call_id,
                status="success",
                result={"temperature_c": 22},
                execution_id=f"e{i}",
            )
            await adapter.send_tool_result(result)
            await adapter.send_tool_result(result)
            assert ws.responses == (1 if i == 0 else 2)
        while True:
            event = await adapter.receive_event()
            if event.event == "assistant_response_end":
                assert event.response_id == "r2" and event.turn_id == "t1"
                break
        sent = [m for m in ws.sent if m["type"] == "conversation.item.create"]
        assert len(sent) == 2 and {m["item"]["call_id"] for m in sent} == {"c0", "c1"}
        await adapter.close()

    asyncio.run(run())


@pytest.mark.parametrize("arguments", ["{", "[]", "null"])
def test_malformed_arguments_are_recorded_not_executed(context, arguments):
    async def run():
        clock = SystemClock()
        mapper = QwenEventMapper(context, MemoryArtifacts(), lambda: CapabilityManifest())
        await mapper.normalize(
            {"type": "response.created", "response": {"id": "r"}}, clock.now(), "raw1"
        )
        data = {
            "type": "response.function_call_arguments.done",
            "response_id": "r",
            "name": "weather",
            "call_id": "c",
            "arguments": arguments,
        }
        events = await mapper.normalize(data, clock.now(), "raw2")
        assert events[-1].payload.valid_json is False
        assert await mapper.normalize(data, clock.now(), "raw3") == []
        with pytest.raises(ValueError, match="conflicting duplicate"):
            await mapper.normalize({**data, "arguments": "{}"}, clock.now(), "raw4")

    asyncio.run(run())
