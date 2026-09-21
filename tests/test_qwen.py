import asyncio
import base64
import json

import pytest
from pydantic import ValidationError

from adapters.base import InterruptRequest, SessionConfig
from adapters.qwen import QwenRealtimeAdapter, QwenSettings
from adapters.qwen.adapter import QwenAdapterError
from adapters.qwen.config import INPUT_FORMAT, OUTPUT_FORMAT
from benchmark.audio import AudioFrame
from benchmark.connection_probe import run_connection_probe
from events.recorder import EventRecorder
from events.replay import read_recording

FAKE_SECRET = "unit-test-placeholder-credential"


def session_config(mode="manual", voice="Tina"):
    return SessionConfig(
        model="qwen3.5-omni-flash-realtime",
        voice=voice,
        turn_mode=mode,
        input_audio=INPUT_FORMAT,
        output_audio=OUTPUT_FORMAT,
    )


class FakeSocket:
    def __init__(self, *, auto_response=True, bad_audio=False, voice_error=False, duplicate=False):
        self.queue = asyncio.Queue()
        self.sent = []
        self.auto_response, self.bad_audio, self.voice_error, self.duplicate = (
            auto_response,
            bad_audio,
            voice_error,
            duplicate,
        )
        self.session = {}
        self.close_code = None
        self.responded = False
        self.serial = 0
        self.push({"type": "session.created", "session": {"id": "vendor-session"}})

    def push(self, data):
        self.serial += 1
        data = {"event_id": f"vendor-{self.serial}", **data}
        self.queue.put_nowait(json.dumps(data))

    def respond(self):
        if self.responded:
            return
        self.responded = True
        if self.voice_error:
            self.push(
                {
                    "type": "error",
                    "error": {
                        "code": "COMMON_ERROR",
                        "message": "Voice 'Chelsie' is not supported.",
                    },
                }
            )
            return
        if self.session.get("turn_detection"):
            self.push(
                {"type": "input_audio_buffer.speech_started", "item_id": "u1", "audio_start_ms": 0}
            )
            self.push(
                {"type": "input_audio_buffer.speech_stopped", "item_id": "u1", "audio_end_ms": 100}
            )
            self.push({"type": "input_audio_buffer.committed", "item_id": "u1"})
        self.push({"type": "response.created", "response": {"id": "r1", "status": "in_progress"}})
        self.push(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": "u1",
                "transcript": "你叫什么名字？",
            }
        )
        self.push({"type": "future.vendor.event", "unknown": {"preserve": True}})
        self.push(
            {
                "type": "response.audio_transcript.delta",
                "response_id": "r1",
                "item_id": "a1",
                "delta": "你好",
            }
        )
        chunk = {
            "type": "response.audio.delta",
            "response_id": "r1",
            "item_id": "a1",
            "delta": "not base64!!"
            if self.bad_audio
            else base64.b64encode(b"\x01\x00" * 480).decode(),
        }
        self.push({**chunk, "event_id": "chunk-1"})
        if self.duplicate:
            self.push({**chunk, "event_id": "chunk-1"})
        self.push({**chunk, "event_id": "chunk-2"})
        self.push(
            {"type": "response.audio_transcript.done", "response_id": "r1", "transcript": "你好"}
        )
        self.push({"type": "response.audio.done", "response_id": "r1"})
        self.push(
            {
                "type": "response.done",
                "response": {"id": "r1", "status": "completed", "usage": {"total_tokens": 42}},
            }
        )

    async def recv(self):
        value = await self.queue.get()
        if value is None:
            raise EOFError("connection closed")
        return value

    async def send(self, text):
        data = json.loads(text)
        self.sent.append(data)
        if data["type"] == "session.update":
            self.session = data["session"]
            echoed = {"id": "vendor-session", **self.session}
            if echoed.get("turn_detection") is None:
                echoed.pop("turn_detection", None)  # Observed live: null fields may be omitted.
            self.push({"type": "session.updated", "session": echoed})
        elif (
            data["type"] == "input_audio_buffer.append"
            and self.session.get("turn_detection")
            and self.auto_response
        ):
            self.respond()
        elif data["type"] == "input_audio_buffer.commit":
            self.push({"type": "input_audio_buffer.committed", "item_id": "u1"})
        elif data["type"] == "response.create" and self.auto_response:
            self.respond()
        elif data["type"] == "response.cancel":
            self.push(
                {
                    "type": "response.done",
                    "response": {
                        "id": "r1",
                        "status": "cancelled",
                        "status_details": {"reason": "client_cancelled", "type": "cancelled"},
                    },
                }
            )
        elif data["type"] == "session.finish":
            self.push({"type": "session.finished"})

    async def close(self):
        self.close_code = 1000
        self.queue.put_nowait(None)


@pytest.fixture
def credential(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", FAKE_SECRET)


def factory_for(ws, **settings):
    async def connect(url, **kwargs):
        assert url.startswith("wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=")
        assert kwargs["additional_headers"] == {"Authorization": "Bearer " + FAKE_SECRET}
        return ws

    def factory(context, sink, clock):
        return QwenRealtimeAdapter(
            context,
            sink,
            clock=clock,
            connector=connect,
            settings=QwenSettings(request_timeout_s=1, close_timeout_s=0.1, **settings),
        )

    return factory


def test_credentials_are_environment_only_and_endpoint_is_verified(monkeypatch, context, clock):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="DASHSCOPE_API_KEY"):
        QwenRealtimeAdapter(context, None, clock=clock)
    with pytest.raises(ValidationError):
        QwenSettings(endpoint="wss://unverified.invalid/realtime")


@pytest.mark.parametrize("mode", ["manual", "server_vad"])
def test_complete_probe_records_real_protocol_shapes_offline(
    tmp_path, scenario_data, credential, mode
):
    async def run():
        ws = FakeSocket(duplicate=True)
        result = await run_connection_probe(
            factory_for(ws),
            audio_path=tmp_path / "input.wav",
            output=tmp_path / "probe",
            config=session_config(mode),
            secrets=(FAKE_SECRET,),
            tail_silence_ms=20,
            response_timeout_s=2,
        )
        assert result["success"] and result["received_audio_chunks"] == 2
        assert result["backend"]["websocket_close_code"] == 1000
        recording = read_recording(tmp_path / "probe")
        names = [row["type"] for row in ws.sent]
        assert names.count("input_audio_buffer.commit") == (1 if mode == "manual" else 0)
        assert names.count("response.create") == (1 if mode == "manual" else 0)
        assert names.count("session.finish") == 0
        chunks = [e for e in recording.events if e.event == "assistant_audio_chunk"]
        first = next(e for e in recording.events if e.event == "assistant_audio_start")
        assert first.timestamp_monotonic_ns == chunks[0].timestamp_monotonic_ns
        assert all(e.turn_id == "t1" for e in chunks)
        assert all(e.payload.audio_ref.sample_rate_hz == 24000 for e in chunks)
        effective = json.loads((tmp_path / "probe" / "session_config.json").read_text())
        if mode == "manual":
            assert "turn_detection" in effective["unverified"]
        assert not any(e.event == "interrupt_detected" for e in recording.events)
        raw_types = [e.vendor_event_type for e in recording.raw_events]
        assert raw_types.count("response.audio.delta") == 3
        assert "future.vendor.event" in raw_types
        for path in (tmp_path / "probe").glob("*.json*"):
            assert FAKE_SECRET not in path.read_text()
        assert recording.events[-1].event == "session_end"
        assert set(recording.manifest["files"]) >= {
            "input.wav",
            "output_received.wav",
            "transcript.json",
            "probe.json",
            "session_config.json",
        }

    asyncio.run(run())


@pytest.mark.parametrize("fault", ["bad_audio", "voice_error"])
def test_protocol_and_deferred_model_errors_leave_failed_artifacts(
    tmp_path, scenario_data, credential, fault
):
    async def run():
        ws = FakeSocket(**{fault: True})
        result = await run_connection_probe(
            factory_for(ws),
            audio_path=tmp_path / "input.wav",
            output=tmp_path / "probe",
            config=session_config(voice="Chelsie" if fault == "voice_error" else "Tina"),
            secrets=(FAKE_SECRET,),
            response_timeout_s=2,
        )
        assert not result["success"] and result["received_audio_chunks"] == 0
        recording = read_recording(tmp_path / "probe", allow_partial=True)
        assert recording.manifest["status"] == "incomplete"
        assert any(e.event == "error" for e in recording.events)
        assert not any(e.event == "assistant_audio_start" for e in recording.events)
        assert any(e.vendor_event_type == "session.updated" for e in recording.raw_events)

    asyncio.run(run())


def test_cancel_requires_matching_active_response_and_keeps_late_audio(
    tmp_path, context, clock, event, credential
):
    async def run():
        ws = FakeSocket(auto_response=False)
        async with EventRecorder(
            tmp_path / "case", context, clock=clock, secrets=(FAKE_SECRET,)
        ) as recorder:
            adapter = factory_for(ws)(context, recorder, clock)
            await adapter.connect()
            await adapter.configure(session_config())
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
            await adapter.commit_turn("t1")
            ws.push({"type": "response.created", "response": {"id": "r1", "status": "in_progress"}})
            while True:
                e = await adapter.receive_event()
                await recorder.record(e)
                if e.event == "assistant_response_start":
                    break
            with pytest.raises(QwenAdapterError, match="cancel_target_mismatch"):
                await adapter.interrupt(InterruptRequest(target_response_id="wrong", reason="test"))
            await adapter.interrupt(
                InterruptRequest(target_response_id="r1", reason="control probe")
            )
            while True:
                e = await adapter.receive_event()
                await recorder.record(e)
                if e.event == "assistant_cancelled":
                    break
            ws.push(
                {
                    "type": "response.audio.delta",
                    "response_id": "r1",
                    "delta": base64.b64encode(b"\x01\x00" * 480).decode(),
                }
            )
            await adapter.close()
            while True:
                try:
                    await recorder.record(await adapter.receive_event())
                except EOFError:
                    break
        recording = read_recording(tmp_path / "case")
        assert next(
            e for e in recording.events if e.event == "assistant_audio_chunk"
        ).payload.late_after_cancel
        assert (
            next(e for e in recording.events if e.event == "assistant_cancelled").payload.initiator
            == "client"
        )
        request = next(e for e in recording.events if e.event == "interrupt_requested")
        raw = next(e for e in recording.raw_events if e.vendor_event_type == "response.cancel")
        assert request.payload.command_id == raw.body["event_id"]
        assert not any(e.event == "interrupt_detected" for e in recording.events)

    asyncio.run(run())


def test_overflow_is_fatal_instead_of_silently_dropping_events(
    tmp_path, context, clock, credential
):
    async def run():
        ws = FakeSocket(auto_response=False)
        recorder = EventRecorder(tmp_path / "case", context, clock=clock)
        await recorder.__aenter__()
        adapter = factory_for(ws, queue_capacity=1)(context, recorder, clock)
        await adapter.connect()  # session_start fills the queue, deliberately no consumer.
        with pytest.raises(QwenAdapterError, match="event_queue_overflow"):
            await adapter.configure(session_config())
        await adapter.close()
        await recorder.close(complete=False)
        assert adapter.diagnostics()["fatal_error_code"] == "event_queue_overflow"

    asyncio.run(run())


def test_disconnect_wakes_waiter_and_emits_abnormal_session_end(
    tmp_path, context, clock, credential
):
    async def run():
        ws = FakeSocket(auto_response=False)
        recorder = EventRecorder(tmp_path / "case", context, clock=clock)
        await recorder.__aenter__()
        adapter = factory_for(ws)(context, recorder, clock)
        await adapter.connect()
        await adapter.configure(session_config())
        await ws.close()
        seen = []
        with pytest.raises(QwenAdapterError, match="connection_closed"):
            async with asyncio.timeout(2):
                while True:
                    e = await adapter.receive_event()
                    seen.append(e)
                    await recorder.record(e)
        await adapter.close()
        await recorder.close(complete=False)
        assert next(e for e in seen if e.event == "session_end").payload.complete is False

    asyncio.run(run())
