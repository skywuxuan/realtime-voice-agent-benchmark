import asyncio
import hashlib
import io
import wave
from pathlib import Path

from adapters.base import (
    Capability,
    CapabilityManifest,
    EffectiveConfig,
    RealtimeModelAdapter,
    SendReceipt,
    SessionConfig,
    SessionInfo,
)
from benchmark.audio import AudioFormat
from benchmark.backchannel import run_backchannel_case
from benchmark.config import LatencyProfile
from evaluator.backchannel import aggregate, evaluate_case
from events.schema import EventDraft, RecordingContext
from scenarios.schema import Scenario


def wav(samples: int, value: int) -> bytes:
    stream = io.BytesIO()
    with wave.open(stream, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes((value.to_bytes(2, "little", signed=True)) * samples)
    return stream.getvalue()


class BackchannelFixtureAdapter(RealtimeModelAdapter):
    def __init__(self, context, sink, clock):
        super().__init__()
        self.context = context
        self.sink = sink
        self.clock = clock
        self.queue = asyncio.Queue()
        self.started = False

    def capabilities(self):
        supported = Capability(
            status="supported", verification="experiment", evidence=("deterministic fixture",)
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

    def event(
        self, kind, payload, *, source="assistant", turn_id=None, response_id=None, stream_id=None
    ):
        return EventDraft(
            **self.context.model_dump(),
            **self.clock.now().model_dump(),
            event=kind,
            source=source,
            producer="fixture.backchannel",
            turn_id=turn_id,
            response_id=response_id,
            stream_id=stream_id,
            timing={"basis": "client_receive"},
            payload=payload,
        )

    async def _connect(self):
        self.queue.put_nowait(
            self.event(
                "session_start",
                {
                    "vendor_session_id": None,
                    "adapter_version": "fixture",
                    "capabilities": self.capabilities().model_dump(mode="json"),
                },
                source="system",
            )
        )
        return SessionInfo(
            session_id=self.context.session_id, vendor_session_id=None, adapter_version="fixture"
        )

    async def _configure(self, config):
        self.queue.put_nowait(
            self.event(
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
        if frame.turn_id == "t1" and not self.started:
            self.started = True
            rid = "r1"
            pcm = b"\x01\x00" * 24000
            ref = await self.sink.store_audio(rid, pcm, AudioFormat(sample_rate_hz=24000))
            chunk = self.event(
                "assistant_audio_chunk",
                {"audio_ref": ref.model_dump(), "chunk_index": 0},
                turn_id="t1",
                response_id=rid,
                stream_id=rid,
            )
            self.queue.put_nowait(
                self.event(
                    "assistant_response_start",
                    {
                        "response_status": "in_progress",
                        "trigger_turn_id": "t1",
                        "association_method": "vendor_ids",
                    },
                    turn_id="t1",
                    response_id=rid,
                )
            )
            self.queue.put_nowait(
                self.event(
                    "assistant_audio_start",
                    {
                        "first_chunk_event_id": chunk.event_id,
                        "audio_format": AudioFormat(sample_rate_hz=24000).model_dump(),
                    },
                    turn_id="t1",
                    response_id=rid,
                    stream_id=rid,
                )
            )
            self.queue.put_nowait(chunk)
        elif frame.turn_id == "t2":
            self.queue.put_nowait(
                self.event(
                    "assistant_audio_end",
                    {
                        "reason": "completed",
                        "last_chunk_event_id": None,
                        "complete": True,
                        "completion_source": "fixture",
                    },
                    turn_id="t1",
                    response_id="r1",
                    stream_id="r1",
                )
            )
            self.queue.put_nowait(
                self.event(
                    "assistant_response_end",
                    {"status": "completed", "completion_source": "fixture"},
                    turn_id="t1",
                    response_id="r1",
                )
            )
        return SendReceipt(
            stream_id=frame.stream_id,
            chunk_index=frame.chunk_index,
            byte_count=len(frame.pcm),
            started=started,
            completed=self.clock.now(),
        )

    async def _commit_turn(self, turn_id):
        return None

    async def _receive_event(self):
        item = await self.queue.get()
        if item is None:
            raise EOFError("fixture closed")
        return item

    async def _close(self):
        self.queue.put_nowait(
            self.event(
                "session_end",
                {"reason": "fixture_close", "complete": True, "last_response_ids": ()},
                source="system",
            )
        )
        self.queue.put_nowait(None)


def scenario_data(initial, backchannel):
    return Scenario.model_validate(
        {
            "schema_version": "0.1",
            "scenario_id": "fixture_backchannel",
            "scenario_version": 1,
            "suite": "realtime",
            "category": "backchannel",
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
            "audio": {
                "chunk_ms": 20,
                "assets": {
                    "initial": {
                        "path": "initial.wav",
                        "sha256": hashlib.sha256(initial).hexdigest(),
                        "reference_text": "请介绍北京。",
                        "speech_bounds_samples": [0, 1600],
                        "sample_rate_hz": 16000,
                        "provenance": {"kind": "synthetic_fixture", "speaker_id": "fixture"},
                        "boundary_annotation": {"method": "fixture", "status": "synthetic"},
                    },
                    "backchannel": {
                        "path": "backchannel.wav",
                        "sha256": hashlib.sha256(backchannel).hexdigest(),
                        "reference_text": "嗯嗯",
                        "speech_bounds_samples": [0, 320],
                        "sample_rate_hz": 16000,
                        "provenance": {"kind": "synthetic_fixture", "speaker_id": "fixture"},
                        "boundary_annotation": {"method": "fixture", "status": "synthetic"},
                    },
                },
            },
            "actions": [
                {
                    "action_id": "ask",
                    "type": "play_audio",
                    "asset": "initial",
                    "turn_id": "t1",
                    "stimulus": "utterance",
                    "trigger": {"type": "session_ready"},
                },
                {
                    "action_id": "ack",
                    "type": "play_audio",
                    "asset": "backchannel",
                    "turn_id": "t2",
                    "stimulus": "backchannel",
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
                        {"type": "minimum_continuation_evidence", "remaining_ms": 200},
                    ],
                },
            ],
            "oracle": {
                "stimulus": "backchannel",
                "metric_profile": "fixture",
                "assertions": [{"type": "continues_response"}],
            },
            "termination": {
                "max_case_duration_ms": 5000,
                "response_timeout_ms": 1000,
                "post_stimulus_observation_ms": 2000,
                "drain_timeout_ms": 3000,
            },
        }
    )


def test_backchannel_runner_and_evaluator(tmp_path: Path):
    initial, backchannel = wav(1600, 100), wav(320, 200)
    scenario = scenario_data(initial, backchannel)
    context = RecordingContext(
        run_id="run", scenario_id=scenario.scenario_id, attempt_id="a", session_id="s"
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

    def factory(context, sink, clock):
        return BackchannelFixtureAdapter(context, sink, clock)

    async def run():
        return await run_backchannel_case(
            factory,
            scenario=scenario,
            source_wavs={"initial.wav": initial, "backchannel.wav": backchannel},
            output=tmp_path / "case",
            context=context,
            config=config,
            profile=profile,
        )

    trial = asyncio.run(run())
    result = evaluate_case(tmp_path / "case")
    assert trial["status"] == "completed"
    assert result["status"] == "pass"
    assert (
        result["eligible"]
        and result["false_interruption"] is False
        and result["response_continued"] is True
    )
    summary = aggregate([result])
    assert summary["realtime"]["groups"][0]["false_interruption_rate"] == 0


def test_backchannel_failures_do_not_become_continuation_success(tmp_path):
    from test_interruption import FixtureInterruptionAdapter, interruption_setup

    from events.replay import read_recording

    original, config, profile, source = interruption_setup()
    data = original.model_dump(mode="json")
    data["category"] = "backchannel"
    data["actions"][1]["stimulus"] = "backchannel"
    data["oracle"] = {
        "stimulus": "backchannel",
        "metric_profile": "fixture",
        "assertions": [{"type": "continues_response"}],
    }
    scenario = Scenario.model_validate(data)
    results = []
    for mode, expected in (
        ("normal", "fail"),
        ("client_cancel", "invalid"),
        ("no_stop", "unknown"),
        ("partial_close", "invalid"),
    ):
        context = RecordingContext(
            run_id="r", scenario_id=scenario.scenario_id, attempt_id=mode, session_id="s"
        )

        def factory(context, sink, clock):
            return FixtureInterruptionAdapter(context, sink, clock, mode)

        asyncio.run(
            run_backchannel_case(
                factory,
                scenario=scenario,
                source_wavs=source,
                output=tmp_path / mode,
                context=context,
                config=config,
                profile=profile,
            )
        )
        result = evaluate_case(tmp_path / mode)
        assert result["status"] == expected, result
        results.append(result)
        recording = read_recording(tmp_path / mode, allow_partial=mode == "partial_close")
        assert any(e.event == "backchannel_end" for e in recording.events)
        assert not any(e.event == "interrupt_start" for e in recording.events)
    group = aggregate(results)["realtime"]["groups"][0]
    assert group["counts"]["eligible"] == 2
    assert group["false_interruption_known_count"] == 1
    assert group["false_interruption_unknown_count"] == 1
    assert group["false_interruption_rate_bounds"] == [0.5, 1.0]
