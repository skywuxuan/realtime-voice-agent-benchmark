import asyncio
import hashlib
import io
import wave

import pytest

from adapters.base import (
    Capability,
    CapabilityManifest,
    EffectiveConfig,
    RealtimeModelAdapter,
    SendReceipt,
    SessionConfig,
    SessionInfo,
)
from adapters.qwen.protocol import QwenEventMapper
from adapters.testing import ScriptedAdapter
from benchmark.audio import AudioFormat, AudioRef
from benchmark.config import LatencyProfile
from benchmark.evaluate import evaluate_run
from benchmark.interruption import _residual_audio_ms, run_interruption_case
from benchmark.run import run_suite
from evaluator.interruption import aggregate, evaluate_case
from events.clock import ClockReading, SystemClock
from events.replay import file_hash, read_recording
from events.schema import EventDraft, RecordingContext
from scenarios.schema import AudioAsset, PlayAudio, Scenario
from simulator.input import send_utterance
from simulator.playback import VirtualPlayback


class MemorySink:
    def __init__(self):
        self.audio_data = {}

    async def record_raw(self, raw):
        return raw

    async def store_audio(self, stream_id, pcm, format):
        from benchmark.audio import AudioRef

        offset = len(self.audio_data.get(stream_id, b""))
        self.audio_data[stream_id] = self.audio_data.get(stream_id, b"") + pcm
        return AudioRef(
            **format.model_dump(),
            path=f"audio/{stream_id}.pcm",
            byte_offset=offset,
            byte_length=len(pcm),
            sample_offset=offset // format.bytes_per_sample_frame,
            sample_count=len(pcm) // format.bytes_per_sample_frame,
        )


def reading(ns):
    return ClockReading(
        clock_id="test", timestamp_monotonic_ns=ns, wall_clock_timestamp="2026-09-18T00:00:00Z"
    )


def test_qwen_mapper_only_confirms_interruption_after_vad_and_cancel():
    async def run():
        context = RecordingContext(run_id="run", scenario_id="case", attempt_id="a", session_id="s")
        sink = MemorySink()
        mapper = QwenEventMapper(
            context, sink, lambda: type("C", (), {"model_dump": lambda self, **_: {}})()
        )
        mapper.turn_mode = "server_vad"
        await mapper.normalize(
            {"type": "session.created", "session": {"id": "vendor"}}, reading(1), "raw0"
        )
        mapper.note_input("t1")
        await mapper.normalize(
            {"type": "input_audio_buffer.committed", "item_id": "u1"}, reading(2), "raw1"
        )
        await mapper.normalize(
            {"type": "response.created", "response": {"id": "r1", "status": "in_progress"}},
            reading(3),
            "raw2",
        )
        audio = "AQABAA=="
        first = await mapper.normalize(
            {"type": "response.audio.delta", "response_id": "r1", "item_id": "a1", "delta": audio},
            reading(4),
            "raw3",
        )
        assert any(event.event == "assistant_audio_start" for event in first)
        vad = await mapper.normalize(
            {"type": "input_audio_buffer.speech_started", "item_id": "u2", "audio_start_ms": 100},
            reading(5),
            "raw4",
        )
        assert [event.event for event in vad] == ["vad_start"]
        cancelled = await mapper.normalize(
            {
                "type": "response.done",
                "response": {
                    "id": "r1",
                    "status": "cancelled",
                    "status_details": {"reason": "server_vad"},
                },
            },
            reading(6),
            "raw5",
        )
        assert any(event.event == "assistant_cancelled" for event in cancelled)
        detected = next(event for event in cancelled if event.event == "interrupt_detected")
        assert detected.payload.evidence_level == "confirmed"
        assert detected.response_id == "r1"
        mapper.note_input("t1")
        mapper.note_input("t2")
        await mapper.normalize(
            {"type": "input_audio_buffer.committed", "item_id": "u2"}, reading(7), "raw7"
        )
        second = await mapper.normalize(
            {"type": "response.created", "response": {"id": "r2", "status": "in_progress"}},
            reading(8),
            "raw8",
        )
        assert second[0].turn_id == "t2"

    asyncio.run(run())


def test_qwen_mapper_links_vad_arriving_after_server_cancel():
    async def run():
        context = RecordingContext(run_id="run", scenario_id="case", attempt_id="a", session_id="s")
        mapper = QwenEventMapper(
            context, MemorySink(), lambda: type("C", (), {"model_dump": lambda self, **_: {}})()
        )
        mapper.turn_mode = "server_vad"
        mapper.note_input("t1")
        await mapper.normalize(
            {"type": "input_audio_buffer.committed", "item_id": "u1"}, reading(1), "raw1"
        )
        await mapper.normalize(
            {"type": "response.created", "response": {"id": "r1", "status": "in_progress"}},
            reading(2),
            "raw2",
        )
        cancelled = await mapper.normalize(
            {
                "type": "response.done",
                "response": {
                    "id": "r1",
                    "status": "cancelled",
                    "status_details": {"reason": "turn_detected"},
                },
            },
            reading(3),
            "raw3",
        )
        assert not any(event.event == "interrupt_detected" for event in cancelled)
        late_vad = await mapper.normalize(
            {"type": "input_audio_buffer.speech_started", "item_id": "u2"},
            reading(4),
            "raw4",
        )
        assert [event.event for event in late_vad] == ["vad_start", "interrupt_detected"]

        mapper.note_input("t2")
        await mapper.normalize(
            {"type": "input_audio_buffer.committed", "item_id": "u3"}, reading(5), "raw5"
        )
        await mapper.normalize(
            {"type": "response.created", "response": {"id": "r2", "status": "in_progress"}},
            reading(6),
            "raw6",
        )
        client_cancelled = await mapper.normalize(
            {
                "type": "response.done",
                "response": {
                    "id": "r2",
                    "status": "cancelled",
                    "status_details": {"reason": "client_cancelled"},
                },
            },
            reading(7),
            "raw7",
        )
        late_client_vad = await mapper.normalize(
            {"type": "input_audio_buffer.speech_started", "item_id": "u4"},
            reading(8),
            "raw8",
        )
        assert not any(
            event.event == "interrupt_detected" for event in client_cancelled + late_client_vad
        )

    asyncio.run(run())


def test_interruption_aggregate_keeps_unknown_and_null_stop_latency():
    base = {
        "scenario_id": "case",
        "attempt_id": "a",
        "warmup": False,
        "eligible": True,
        "status": "unknown",
        "interruption_detected": False,
        "evidence_level": None,
        "stop_latency_ms": None,
        "residual_audio_duration_ms": None,
        "context_switch": "unknown",
        "cleanup_warnings": [],
        "group": {"model": "fixture"},
        "reasons": ["missing"],
    }
    base["status"] = "invalid"
    result = aggregate([base])
    group = result["realtime"]["groups"][0]
    assert group["interruption_detection_rate"] is None
    assert group["stop_latency_ms"]["n"] == 0
    assert group["stop_latency_ms"]["p95"] is None
    assert group["counts"]["invalid"] == 1
    assert result["realtime"]["counts"]["eligible"] == 0


def test_virtual_playback_stops_on_cancel_and_drops_queued_audio():
    async def run():
        context = RecordingContext(run_id="run", scenario_id="case", attempt_id="a", session_id="s")

        class Sink:
            def __init__(self):
                self.data = b"\1\0" * 960

            def audio(self, ref):
                return self.data

        sink = Sink()
        events = []
        playback = VirtualPlayback(
            context, SystemClock(), sink, events.append, LatencyProfile(playback_chunk_ms=20)
        )
        ref = AudioRef(
            **AudioFormat(sample_rate_hz=24000).model_dump(),
            path="audio/r.pcm",
            byte_offset=0,
            byte_length=len(sink.data),
            sample_offset=0,
            sample_count=960,
        )
        chunk = __import__("events.schema", fromlist=["EventDraft"]).EventDraft(
            **context.model_dump(),
            **SystemClock().now().model_dump(),
            event="assistant_audio_chunk",
            source="assistant",
            producer="test",
            response_id="r1",
            stream_id="r1",
            timing={"basis": "client_receive"},
            payload={"audio_ref": ref.model_dump(), "chunk_index": 0},
        )
        playback.submit(chunk)
        await asyncio.sleep(0.002)
        cancelled = chunk.model_copy(
            update={
                "event": "assistant_cancelled",
                "payload": {
                    "target_response_id": "r1",
                    "initiator": "server",
                    "reason": "server_vad",
                    "evidence": [chunk.event_id],
                },
            }
        )
        playback.submit(cancelled)
        await playback.finish()
        assert any(event.event == "assistant_playback_stop" for event in events)
        assert any(event.event == "audio_chunk_dropped" for event in events) or any(
            event.event == "assistant_playback_chunk" for event in events
        )

    asyncio.run(run())


def test_virtual_playback_abort_bounds_long_followup_audio():
    async def run():
        context = RecordingContext(run_id="run", scenario_id="case", attempt_id="a", session_id="s")

        class Sink:
            def audio(self, ref):
                return b"\1\0" * ref.sample_count

        events = []
        playback = VirtualPlayback(
            context,
            SystemClock(),
            Sink(),
            events.append,
            LatencyProfile(playback_chunk_ms=20, max_playback_lateness_ms=100),
        )
        ref = AudioRef(
            **AudioFormat(sample_rate_hz=24000).model_dump(),
            path="audio/long.pcm",
            byte_offset=0,
            byte_length=2 * 24000 * 20,
            sample_offset=0,
            sample_count=24000 * 20,
        )
        chunk = EventDraft(
            **context.model_dump(),
            **SystemClock().now().model_dump(),
            event="assistant_audio_chunk",
            source="assistant",
            producer="test",
            response_id="r1",
            stream_id="r1",
            timing={"basis": "client_receive"},
            payload={"audio_ref": ref.model_dump(), "chunk_index": 0},
        )
        playback.submit(chunk)
        await asyncio.sleep(0.002)
        try:
            await asyncio.wait_for(playback.finish(), 0.005)
        except TimeoutError:
            pass
        await playback.abort()
        assert playback._task.done()

    asyncio.run(run())


def test_second_utterance_audio_reference_and_residual_window():
    async def run():
        context = RecordingContext(run_id="run", scenario_id="case", attempt_id="a", session_id="s")
        clock = SystemClock()
        adapter = ScriptedAdapter(context, clock)
        await adapter.connect()
        await adapter.configure(
            SessionConfig(
                model="fixture",
                turn_mode="manual",
                input_audio=AudioFormat(sample_rate_hz=16000),
                output_audio=AudioFormat(sample_rate_hz=24000),
            )
        )
        sink = MemorySink()
        events = []
        asset = AudioAsset(
            path="fixture.wav",
            sha256="0" * 64,
            reference_text="fixture",
            speech_bounds_samples=(0, 320),
            sample_rate_hz=16000,
            provenance={"kind": "synthetic_fixture", "speaker_id": "none"},
        )
        for number in (1, 2):
            await send_utterance(
                adapter,
                sink,
                context,
                clock,
                events.append,
                action=PlayAudio(
                    action_id=f"a{number}",
                    type="play_audio",
                    asset="a",
                    turn_id=f"t{number}",
                    trigger={"type": "session_ready"},
                ),
                asset=asset,
                pcm=bytes((number, 0)) * 320,
                chunk_ms=20,
                profile=LatencyProfile(
                    tail_silence_ms=0, max_send_lateness_ms=100, max_send_duration_ms=100
                ),
            )
        second = next(
            event for event in events if event.event == "user_audio_chunk" and event.turn_id == "t2"
        )
        assert second.payload.audio_ref.byte_offset == 640
        assert sink.audio_data["input"][640:1280] == bytes((2, 0)) * 320
        chunks = [
            type(
                "Chunk",
                (),
                {
                    "event": "assistant_playback_chunk",
                    "response_id": "old",
                    "timestamp_monotonic_ns": start,
                    "payload": type(
                        "Payload", (), {"sample_count": count, "sample_rate_hz": 1000}
                    )(),
                },
            )()
            for start, count in ((0, 100), (100_000_000, 40))
        ]
        assert _residual_audio_ms(chunks, "old", 100_000_000, 140_000_000) == 40

    asyncio.run(run())


class FixtureInterruptionAdapter(RealtimeModelAdapter):
    """Deterministic two-response transport used only for sealed-artifact tests."""

    def __init__(self, context, sink, clock, mode="normal"):
        super().__init__()
        self.mode = mode
        self.correction_task = None
        self.context, self.sink, self.clock = context, sink, clock
        self.queue: asyncio.Queue[EventDraft | None] = asyncio.Queue()
        self.old_task = None
        self.cancel_started = False
        self.old_last_chunk = None

    def capabilities(self):
        supported = Capability(
            status="supported",
            verification="experiment",
            evidence=("in-process deterministic fixture",),
        )
        return CapabilityManifest(
            features={
                name: supported
                for name in (
                    "audio_input",
                    "audio_output",
                    "streaming_input",
                    "streaming_output",
                    "server_vad",
                )
            }
        )

    def _event(
        self, event, payload, *, source="assistant", turn_id=None, response_id=None, stream_id=None
    ):
        return EventDraft(
            **self.context.model_dump(),
            **self.clock.now().model_dump(),
            event=event,
            source=source,
            producer="fixture.interruption",
            turn_id=turn_id,
            response_id=response_id,
            stream_id=stream_id,
            timing={"basis": "client_receive"},
            payload=payload,
        )

    def _queue(self, event):
        if self.mode == "partial_close" and event.event == "session_end":
            return
        if (
            self.mode == "no_audio"
            and event.response_id == "new_response"
            and event.event.startswith("assistant_audio_")
        ):
            return
        if self.mode in {"timeout", "no_stop"} and event.response_id == "new_response":
            return
        if (
            self.mode == "no_stop"
            and event.response_id == "old_response"
            and event.event
            in {
                "assistant_response_end",
                "assistant_cancelled",
                "assistant_audio_end",
                "interrupt_detected",
            }
        ):
            return
        if self.mode == "client_cancel" and event.event == "assistant_cancelled":
            event = EventDraft.model_validate(
                {
                    **event.model_dump(),
                    "payload": {
                        **event.payload.model_dump(),
                        "initiator": "client",
                        "reason": "client_cancelled",
                    },
                }
            )
        if (
            self.mode == "new_failed"
            and event.event == "assistant_response_end"
            and event.response_id == "new_response"
        ):
            event = EventDraft.model_validate(
                {
                    **event.model_dump(),
                    "payload": {**event.payload.model_dump(), "status": "failed"},
                }
            )
        self.queue.put_nowait(event)

    async def _connect(self):
        self._queue(
            self._event(
                "session_start",
                {
                    "vendor_session_id": None,
                    "adapter_version": "fixture-interruption-0.1",
                    "capabilities": self.capabilities().model_dump(mode="json"),
                },
                source="system",
            )
        )
        return SessionInfo(
            session_id=self.context.session_id,
            vendor_session_id=None,
            adapter_version="fixture-interruption-0.1",
        )

    async def _configure(self, config):
        self._queue(
            self._event(
                "session_configured",
                {
                    "requested": config.model_dump(mode="json"),
                    "effective": config.model_dump(mode="json"),
                    "unverified": {},
                },
                source="system",
            )
        )
        return EffectiveConfig(
            requested=config.model_dump(mode="json"),
            effective=config.model_dump(mode="json"),
            unverified={},
        )

    async def _send_audio(self, frame):
        started = self.clock.now()
        if frame.turn_id == "t1" and self.old_task is None:
            self.old_task = asyncio.create_task(self._produce_old_response())
        elif frame.turn_id == "t2" and not self.cancel_started:
            self.cancel_started = True
            if self.old_task:
                self.old_task.cancel()
                await asyncio.gather(self.old_task, return_exceptions=True)
            if self.mode == "disconnect":
                self.queue.put_nowait(None)
            else:
                self.correction_task = asyncio.create_task(self._produce_correction())
        return SendReceipt(
            stream_id=frame.stream_id,
            chunk_index=frame.chunk_index,
            byte_count=len(frame.pcm),
            started=started,
            completed=self.clock.now(),
        )

    async def _produce_old_response(self):
        response_id, turn_id = "old_response", "t1"
        self._queue(
            self._event(
                "assistant_response_start",
                {
                    "response_status": "in_progress",
                    "trigger_turn_id": turn_id,
                    "association_method": "vendor_ids",
                },
                turn_id=turn_id,
                response_id=response_id,
            )
        )
        for index in range(2):
            pcm = b"\x01\x00" * 24000  # Buffered continuation, not past playback.
            ref = await self.sink.store_audio(response_id, pcm, AudioFormat(sample_rate_hz=24000))
            chunk = self._event(
                "assistant_audio_chunk",
                {"audio_ref": ref.model_dump(), "chunk_index": index},
                turn_id=turn_id,
                response_id=response_id,
                stream_id=response_id,
            )
            if index == 0:
                self._queue(
                    self._event(
                        "assistant_audio_start",
                        {
                            "first_chunk_event_id": chunk.event_id,
                            "audio_format": AudioFormat(sample_rate_hz=24000).model_dump(),
                        },
                        turn_id=turn_id,
                        response_id=response_id,
                        stream_id=response_id,
                    )
                )
            self.old_last_chunk = chunk.event_id
            self._queue(chunk)
            await asyncio.sleep(1)

    async def _produce_correction(self):
        old_id, new_id = "old_response", "new_response"
        vad = self._event(
            "vad_start", {"detector": "fixture"}, source="user", turn_id="t2", stream_id="input"
        )
        self._queue(vad)
        old_end = self._event(
            "assistant_response_end",
            {"status": "cancelled", "completion_source": "fixture"},
            turn_id="t1",
            response_id=old_id,
        )
        self._queue(old_end)
        self._queue(
            self._event(
                "assistant_cancelled",
                {
                    "target_response_id": old_id,
                    "initiator": "server",
                    "reason": "server_vad",
                    "evidence": [old_end.event_id],
                },
                turn_id="t1",
                response_id=old_id,
            )
        )
        self._queue(
            self._event(
                "interrupt_detected",
                {
                    "target_response_id": old_id,
                    "mechanism": "fixture_vad",
                    "evidence_event_ids": [vad.event_id, old_end.event_id],
                    "evidence_level": "confirmed",
                },
                turn_id="t1",
                response_id=old_id,
            )
        )
        self._queue(
            self._event(
                "assistant_audio_end",
                {
                    "reason": "cancelled",
                    "last_chunk_event_id": self.old_last_chunk,
                    "complete": False,
                    "completion_source": "fixture",
                },
                turn_id="t1",
                response_id=old_id,
                stream_id=old_id,
            )
        )
        await asyncio.sleep(0.03)
        if self.mode == "late_audio":
            ref = await self.sink.store_audio(
                old_id, b"\x03\x00" * 480, AudioFormat(sample_rate_hz=24000)
            )
            self._queue(
                self._event(
                    "assistant_audio_chunk",
                    {"audio_ref": ref.model_dump(), "chunk_index": 9, "late_after_cancel": True},
                    turn_id="t1",
                    response_id=old_id,
                    stream_id=old_id,
                )
            )
        self._queue(
            self._event(
                "assistant_response_start",
                {
                    "response_status": "in_progress",
                    "trigger_turn_id": "t2",
                    "association_method": "vendor_ids",
                },
                turn_id="t2",
                response_id=new_id,
            )
        )
        self._queue(
            self._event(
                "assistant_text_done",
                {
                    "text": "好的，我来介绍上海。",
                    "channel": "spoken_transcript",
                    "completion_source": "fixture",
                },
                turn_id="t2",
                response_id=new_id,
            )
        )
        pcm = b"\x02\x00" * (24000 if self.mode == "truncated" else 960)
        ref = await self.sink.store_audio(new_id, pcm, AudioFormat(sample_rate_hz=24000))
        chunk = self._event(
            "assistant_audio_chunk",
            {"audio_ref": ref.model_dump(), "chunk_index": 0},
            turn_id="t2",
            response_id=new_id,
            stream_id=new_id,
        )
        self._queue(
            self._event(
                "assistant_audio_start",
                {
                    "first_chunk_event_id": chunk.event_id,
                    "audio_format": AudioFormat(sample_rate_hz=24000).model_dump(),
                },
                turn_id="t2",
                response_id=new_id,
                stream_id=new_id,
            )
        )
        self._queue(chunk)
        self._queue(
            self._event(
                "assistant_audio_end",
                {
                    "reason": "completed",
                    "last_chunk_event_id": chunk.event_id,
                    "complete": True,
                    "completion_source": "fixture",
                },
                turn_id="t2",
                response_id=new_id,
                stream_id=new_id,
            )
        )
        self._queue(
            self._event(
                "assistant_response_end",
                {"status": "completed", "completion_source": "fixture"},
                turn_id="t2",
                response_id=new_id,
            )
        )

    async def _commit_turn(self, turn_id):
        return None

    async def _receive_event(self):
        event = await self.queue.get()
        if event is None:
            raise EOFError("fixture closed")
        return event

    async def _close(self):
        if self.correction_task and not self.correction_task.done():
            self.correction_task.cancel()
            await asyncio.gather(self.correction_task, return_exceptions=True)
        if self.old_task and not self.old_task.done():
            self.old_task.cancel()
            await asyncio.gather(self.old_task, return_exceptions=True)
        self._queue(
            self._event(
                "session_end",
                {"reason": "fixture_close", "complete": True, "last_response_ids": ()},
                source="system",
            )
        )
        self.queue.put_nowait(None)


def _wav(sample_count: int, value: int) -> bytes:
    stream = io.BytesIO()
    with wave.open(stream, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes((value.to_bytes(2, "little", signed=True)) * sample_count)
    return stream.getvalue()


def interruption_setup():
    initial, correction = _wav(320, 100), _wav(1600, 200)
    assets = {
        "initial": {
            "path": "initial.wav",
            "sha256": hashlib.sha256(initial).hexdigest(),
            "reference_text": "北京周末游",
            "speech_bounds_samples": [0, 320],
            "sample_rate_hz": 16000,
            "provenance": {"kind": "synthetic_fixture", "speaker_id": "fixture"},
            "boundary_annotation": {"method": "fixture", "status": "synthetic"},
        },
        "interrupt": {
            "path": "interrupt.wav",
            "sha256": hashlib.sha256(correction).hexdigest(),
            "reference_text": "改问上海",
            "speech_bounds_samples": [0, 1600],
            "sample_rate_hz": 16000,
            "provenance": {"kind": "synthetic_fixture", "speaker_id": "fixture"},
            "boundary_annotation": {"method": "fixture", "status": "synthetic"},
        },
    }
    scenario = Scenario.model_validate(
        {
            "schema_version": "0.1",
            "scenario_id": "fixture_interruption_e2e",
            "scenario_version": 1,
            "suite": "realtime",
            "category": "interruption",
            "seed": 1,
            "world": {"now": "2026-09-19T10:00:00+08:00", "timezone": "Asia/Shanghai"},
            "capabilities_required": [
                "audio_input",
                "audio_output",
                "streaming_input",
                "streaming_output",
                "server_vad",
            ],
            "session": {"turn_mode": "server_vad", "control_profile": "native_server"},
            "audio": {"chunk_ms": 20, "assets": assets},
            "actions": [
                {
                    "action_id": "ask",
                    "type": "play_audio",
                    "asset": "initial",
                    "turn_id": "t1",
                    "trigger": {"type": "session_ready"},
                },
                {
                    "action_id": "correct",
                    "type": "play_audio",
                    "asset": "interrupt",
                    "turn_id": "t2",
                    "stimulus": "interruption",
                    "trigger": {
                        "type": "after_event",
                        "event": "assistant_playback_start",
                        "where": {"turn_id": "t1"},
                        "occurrence": 1,
                        "bind": {"target_response_id": "response_id"},
                        "delay_ms": 20,
                        "timeout_ms": 2000,
                    },
                    "preconditions": [
                        {"type": "response_still_playing", "response": "$target_response_id"},
                        {"type": "response_still_generating", "response": "$target_response_id"},
                        {"type": "minimum_continuation_evidence", "remaining_ms": 800},
                    ],
                },
            ],
            "oracle": {
                "stimulus": "interruption",
                "expected_new_intent": {"city": "上海"},
                "forbidden_old_intent": {"city": "北京"},
                "metric_profile": "fixture",
                "assertions": [
                    {"type": "old_response_stops"},
                    {"type": "answer_targets_city", "city": "上海"},
                ],
            },
            "termination": {
                "max_case_duration_ms": 8000,
                "response_timeout_ms": 2000,
                "post_stimulus_observation_ms": 250,
                "drain_timeout_ms": 100,
            },
        }
    )
    config = SessionConfig(
        model="fixture",
        input_audio=AudioFormat(sample_rate_hz=16000),
        output_audio=AudioFormat(sample_rate_hz=24000),
        turn_mode="server_vad",
        control_profile="native_server",
    )
    profile = LatencyProfile(
        tail_silence_ms=0,
        max_send_lateness_ms=100,
        max_send_duration_ms=100,
        max_playback_lateness_ms=100,
    )

    return scenario, config, profile, {"initial.wav": initial, "interrupt.wav": correction}


@pytest.mark.parametrize(
    "mode,status,eligible",
    [
        ("normal", "pass", True),
        ("late_audio", "pass", True),
        ("timeout", "fail", True),
        ("no_stop", "fail", True),
        ("no_audio", "fail", True),
        ("new_failed", "fail", True),
        ("disconnect", "infra_failed", False),
        ("partial_close", "invalid", False),
        ("client_cancel", "invalid", False),
        ("truncated", "unknown", True),
    ],
)
def test_interruption_runner_seals_and_evaluates_fake_two_turn_artifact(
    tmp_path, mode, status, eligible
):
    scenario, config, profile, source_wavs = interruption_setup()
    context = RecordingContext(
        run_id="run_fixture",
        scenario_id=scenario.scenario_id,
        attempt_id="attempt_001",
        session_id="session_fixture",
    )

    def factory(context, sink, clock):
        return FixtureInterruptionAdapter(context, sink, clock, mode)

    async def run():
        return await run_interruption_case(
            factory,
            scenario=scenario,
            source_wavs=source_wavs,
            output=tmp_path / "case",
            context=context,
            config=config,
            profile=profile,
        )

    trial = asyncio.run(run())
    root = tmp_path / "case"
    recording = read_recording(root, allow_partial=mode in {"partial_close", "disconnect"})
    evaluated = evaluate_case(root)
    assert evaluated["status"] == status, (trial, evaluated)
    assert evaluated["eligible"] is eligible
    assert (root / "transcript.json").is_file()
    # All received samples are accounted for, even a cancelled half-consumed chunk.
    for rid in ("old_response", "new_response"):
        received = sum(
            e.payload.audio_ref.sample_count
            for e in recording.events
            if e.event == "assistant_audio_chunk" and e.response_id == rid
        )
        accounted = sum(
            e.payload.audio_ref.sample_count
            for e in recording.events
            if e.event in {"assistant_playback_chunk", "audio_chunk_dropped"}
            and e.response_id == rid
        )
        assert received == accounted
        stops = [
            e
            for e in recording.events
            if e.event == "assistant_playback_stop" and e.response_id == rid
        ]
        assert len(stops) <= 1
        if stops:
            assert not any(
                e.event == "assistant_playback_chunk"
                and e.response_id == rid
                and e.timestamp_monotonic_ns >= stops[0].timestamp_monotonic_ns
                for e in recording.events
            )
    if mode == "no_stop":
        assert evaluated["stop_latency_ms"] is None and evaluated["stop_censored"]
        assert (
            aggregate([evaluated])["realtime"]["groups"][0]["stop_latency_ms"]["censored_count"]
            == 1
        )
    if mode == "truncated":
        assert evaluated["context_switch"] == "unknown"
        assert "playback_drain_timeout_truncated" in evaluated["cleanup_warnings"]
    if mode == "late_audio":
        late = next(
            e
            for e in recording.events
            if e.event == "assistant_audio_chunk" and e.payload.late_after_cancel
        )
        assert any(
            e.event == "audio_chunk_dropped" and e.payload.chunk_event_id == late.event_id
            for e in recording.events
        )
    if mode == "normal":
        second = next(
            e for e in recording.events if e.event == "user_audio_chunk" and e.turn_id == "t2"
        )
        assert second.payload.audio_ref.byte_offset >= 640
        assert recording.audio(second.payload.audio_ref) == b"\xc8\0" * 320
        assert evaluated["context_switch"] == "pass"
        assert evaluated["interruption_detected"]


def test_interruption_suite_re_evaluation_and_mixed_preflight(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    scenario, config, profile, source = interruption_setup()
    for name, data in source.items():
        (tmp_path / name).write_bytes(data)

    class Registration:
        alias = "fixture"

        def factory(self, config):
            return FixtureInterruptionAdapter

    kwargs = dict(
        scenarios=(scenario,),
        source_root=tmp_path,
        registration=Registration(),
        model_config=config,
        profile=profile,
        warmups=0,
    )
    root = tmp_path / "run"
    result = asyncio.run(run_suite(output=root, **kwargs))
    before = {str(p): file_hash(p) for p in (root / "cases").rglob("*") if p.is_file()}
    assert result == evaluate_run(root) == evaluate_run(root)
    assert before == {str(p): file_hash(p) for p in (root / "cases").rglob("*") if p.is_file()}
    assert result["realtime"]["counts"]["pass"] == 1
    # Reject before constructing an adapter, creating an output or sending any audio.
    kwargs["scenarios"] = (scenario, scenario.model_copy(update={"category": "latency"}))
    with pytest.raises(ValueError, match="mixed suites"):
        asyncio.run(run_suite(output=tmp_path / "mixed", **kwargs))
    assert not (tmp_path / "mixed").exists()


@pytest.mark.parametrize(
    "reason,elapsed_ns,client_request",
    [
        ("turn_detected", 2_000_000_000, False),
        ("other_reason", 1, False),
        ("turn_detected", 1, True),
    ],
)
def test_unrelated_late_vad_never_confirms_old_cancel(reason, elapsed_ns, client_request):
    async def run():
        context = RecordingContext(run_id="r", scenario_id="s", attempt_id="a", session_id="i")
        mapper = QwenEventMapper(context, MemorySink(), lambda: CapabilityManifest())
        mapper.note_commit("t1")
        await mapper.normalize(
            {"type": "response.created", "response": {"id": "r1"}}, reading(1), "raw1"
        )
        if client_request:
            mapper.responses["r1"].cancel_request = "client_request"
        await mapper.normalize(
            {
                "type": "response.done",
                "response": {
                    "id": "r1",
                    "status": "cancelled",
                    "status_details": {"reason": reason},
                },
            },
            reading(2),
            "raw2",
        )
        events = await mapper.normalize(
            {"type": "input_audio_buffer.speech_started", "item_id": "new"},
            reading(2 + elapsed_ns),
            "raw3",
        )
        assert not any(e.event == "interrupt_detected" for e in events)

    asyncio.run(run())


@pytest.mark.parametrize("variation", ["insufficient_buffer", "occurrence", "where"])
def test_invalid_stimuli_are_not_scored(tmp_path, variation):
    scenario, config, profile, source = interruption_setup()
    data = scenario.model_dump(mode="json")
    second = data["actions"][1]
    if variation == "insufficient_buffer":
        second["preconditions"][-1]["remaining_ms"] = 5000
    else:
        second["trigger"]["timeout_ms"] = 180
        if variation == "occurrence":
            second["trigger"]["occurrence"] = 2
        else:
            second["trigger"]["where"] = {"turn_id": "nonexistent"}
    scenario = Scenario.model_validate(data)
    context = RecordingContext(
        run_id="r", scenario_id=scenario.scenario_id, attempt_id="a", session_id="i"
    )
    trial = asyncio.run(
        run_interruption_case(
            FixtureInterruptionAdapter,
            scenario=scenario,
            source_wavs=source,
            output=tmp_path / "case",
            context=context,
            config=config,
            profile=profile,
        )
    )
    recording = read_recording(tmp_path / "case")
    assert not any(e.event == "interrupt_start" for e in recording.events)
    assert evaluate_case(tmp_path / "case")["eligible"] is False
    assert trial["reason"] == (
        "interruption_precondition_failed"
        if variation == "insufficient_buffer"
        else "trigger_timeout"
    )


def test_case_group_ignores_asset_rms_but_keeps_model_and_timing(tmp_path):
    scenario, config, profile, source = interruption_setup()
    cases = []
    for i in range(3):
        data = scenario.model_dump(mode="json")
        data["audio"]["assets"]["initial"]["boundary_annotation"]["parameters"] = {"threshold": i}
        candidate = Scenario.model_validate(data)
        used_config = config if i < 2 else config.model_copy(update={"voice": "different"})
        context = RecordingContext(
            run_id="r", scenario_id=candidate.scenario_id, attempt_id=f"a{i}", session_id=f"s{i}"
        )
        root = tmp_path / str(i)
        asyncio.run(
            run_interruption_case(
                FixtureInterruptionAdapter,
                scenario=candidate,
                source_wavs=source,
                output=root,
                context=context,
                config=used_config,
                profile=profile,
            )
        )
        cases.append(evaluate_case(root))
    groups = aggregate(cases)["realtime"]["groups"]
    assert sorted(g["counts"]["eligible"] for g in groups) == [1, 2]


def test_close_and_cancel_race_drops_current_queued_and_late_samples():
    async def run():
        context = RecordingContext(run_id="r", scenario_id="s", attempt_id="a", session_id="i")
        clock = SystemClock()

        class Sink:
            def audio(self, ref):
                return b"\1\0" * ref.sample_count

        events = []
        started = asyncio.Event()

        def publish(event):
            events.append(event)
            if event.event == "assistant_playback_start":
                started.set()

        playback = VirtualPlayback(
            context, clock, Sink(), publish, LatencyProfile(max_playback_lateness_ms=100)
        )

        def event(kind, payload):
            return EventDraft(
                **context.model_dump(),
                **clock.now().model_dump(),
                event=kind,
                source="assistant",
                producer="fixture",
                response_id="old",
                stream_id="old",
                timing={"basis": "client_receive"},
                payload=payload,
            )

        def chunk(index):
            ref = AudioRef(
                **AudioFormat(sample_rate_hz=24000).model_dump(),
                path="audio/r.pcm",
                byte_offset=index * 4800,
                byte_length=4800,
                sample_offset=index * 2400,
                sample_count=2400,
            )
            return event(
                "assistant_audio_chunk", {"audio_ref": ref.model_dump(), "chunk_index": index}
            )

        originals = [chunk(i) for i in range(3)]
        playback.submit(originals[0])
        playback.submit(originals[1])
        await started.wait()
        cancel = event(
            "assistant_cancelled",
            {
                "target_response_id": "old",
                "initiator": "server",
                "reason": "turn_detected",
                "evidence": [originals[0].event_id],
            },
        )
        playback.submit(cancel)
        playback.submit(originals[2])
        playback.submit(
            event(
                "assistant_audio_end",
                {
                    "reason": "cancelled",
                    "last_chunk_event_id": originals[2].event_id,
                    "complete": False,
                    "completion_source": "fixture",
                },
            )
        )
        await asyncio.gather(playback.finish(), playback.abort())
        played = [e for e in events if e.event == "assistant_playback_chunk"]
        dropped = [e for e in events if e.event == "audio_chunk_dropped"]
        assert sum(e.payload.audio_ref.sample_count for e in played + dropped) == 7200
        intervals = sorted(
            (e.payload.audio_ref.sample_offset, e.payload.audio_ref.sample_count)
            for e in played + dropped
        )
        cursor = 0
        for offset, count in intervals:
            assert offset == cursor
            cursor += count
        assert len([e for e in events if e.event == "assistant_playback_stop"]) == 1
        assert len(playback.segments) == len(played)

    asyncio.run(run())
