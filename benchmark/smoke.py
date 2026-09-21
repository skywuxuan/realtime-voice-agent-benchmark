"""Offline Phase 1 recording/replay demo. Synthetic tones, no model API and no scores."""

import argparse
import asyncio
import base64
import hashlib
import json
import math
import struct
import uuid
import wave
from pathlib import Path

import yaml

from adapters.base import SessionConfig
from adapters.testing import ScriptedAdapter
from benchmark.audio import AudioFormat, AudioFrame
from events.clock import SystemClock
from events.recorder import EventRecorder
from events.replay import read_recording
from events.schema import RawEvent, RecordingContext, new_event
from scenarios.loader import load_scenario


def tone(rate: int) -> bytes:
    return b"".join(
        struct.pack("<h", int(600 * math.sin(2 * math.pi * 440 * i / rate)))
        for i in range(rate // 50)
    )


async def run_smoke(output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    input_format = AudioFormat(sample_rate_hz=16000)
    output_format = AudioFormat(sample_rate_hz=24000)
    input_pcm, output_pcm = tone(16000), tone(24000)
    with wave.open(str(output / "fixture.wav"), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(input_pcm)
    scenario_data = {
        "schema_version": "0.1",
        "scenario_id": "synthetic_contract_fixture",
        "scenario_version": 1,
        "suite": "realtime",
        "category": "latency",
        "seed": 17,
        "world": {"now": "2026-09-19T10:00:00+08:00", "timezone": "Asia/Shanghai"},
        "capabilities_required": ["audio_input", "audio_output"],
        "session": {"turn_mode": "manual", "system_prompt": "Synthetic contract fixture only"},
        "audio": {
            "assets": {
                "tone": {
                    "path": "fixture.wav",
                    "sha256": hashlib.sha256((output / "fixture.wav").read_bytes()).hexdigest(),
                    "reference_text": "[synthetic tone; not speech]",
                    "speech_bounds_samples": [0, 320],
                    "sample_rate_hz": 16000,
                    "provenance": {
                        "kind": "synthetic_fixture",
                        "speaker_id": "none",
                        "generator": "sine440",
                    },
                }
            }
        },
        "actions": [
            {
                "action_id": "send",
                "type": "play_audio",
                "asset": "tone",
                "turn_id": "t1",
                "trigger": {"type": "session_ready"},
            }
        ],
        "oracle": {
            "stimulus": "utterance",
            "metric_profile": "fixture_no_scoring",
            "assertions": [{"type": "audio_received"}],
        },
        "termination": {},
    }
    (output / "scenario.yaml").write_text(
        yaml.safe_dump(scenario_data, allow_unicode=True), encoding="utf-8"
    )
    scenario = load_scenario(output / "scenario.yaml", asset_root=output)
    context = RecordingContext(
        run_id=f"fixture_{uuid.uuid4().hex}",
        scenario_id=scenario.scenario_id,
        attempt_id="attempt_001",
        session_id="session_fixture",
    )
    clock = SystemClock()
    adapter = ScriptedAdapter(context, clock)
    config = SessionConfig(
        **scenario.model_session_options().model_dump(),
        model="synthetic-fixture",
        input_audio=input_format,
        output_audio=output_format,
    )
    async with EventRecorder(
        output / "recording",
        context,
        clock=clock,
        config={
            "mode": "synthetic_fixture",
            "scenario_sha256": scenario.sha256,
            "model_config": config.model_dump(mode="json"),
        },
    ) as recorder:
        await adapter.connect()
        await adapter.configure(config)

        async def emit(event: str, payload: dict, **fields):
            draft = new_event(
                context,
                clock,
                event=event,
                payload=payload,
                source=fields.pop("source", "system"),
                producer="synthetic_fixture",
                timing={"basis": fields.pop("basis", "simulator_boundary")},
                **fields,
            )
            return await recorder.record(draft)

        await emit(
            "session_start",
            {
                "vendor_session_id": None,
                "adapter_version": "fixture-0.1",
                "capabilities": adapter.capabilities().model_dump(mode="json"),
            },
        )
        await emit(
            "user_audio_start",
            {
                "action_id": "send",
                "asset_id": "tone",
                "sample_index": 0,
                "annotation_source": "synthetic_fixture",
            },
            source="user",
            turn_id="t1",
            stream_id="input",
        )
        raw_sent = await recorder.record_raw(
            RawEvent(
                **context.model_dump(),
                **clock.now().model_dump(),
                direction="sent",
                transport="fixture",
                vendor_event_type="fixture.audio",
                body={"audio": base64.b64encode(input_pcm).decode()},
            )
        )
        receipt = await adapter.send_audio(
            AudioFrame(
                pcm=input_pcm,
                format=input_format,
                stream_id="input",
                turn_id="t1",
                chunk_index=0,
                sample_offset=0,
            )
        )
        input_ref = await recorder.store_audio("input", input_pcm, input_format)
        await emit(
            "user_audio_chunk",
            {
                "audio_ref": input_ref.model_dump(),
                "chunk_index": 0,
                "silence": False,
                "planned_send_ns": receipt.started.timestamp_monotonic_ns,
                "send_started_ns": receipt.started.timestamp_monotonic_ns,
                "send_completed_ns": receipt.completed.timestamp_monotonic_ns,
            },
            source="user",
            turn_id="t1",
            stream_id="input",
            raw_event_ref=raw_sent.raw_event_id,
        )
        await emit(
            "user_audio_end",
            {
                "action_id": "send",
                "end_sample": 320,
                "annotation_source": "synthetic_fixture_unpaced",
            },
            source="user",
            turn_id="t1",
            stream_id="input",
        )
        await adapter.commit_turn("t1")
        await emit(
            "assistant_response_start",
            {
                "response_status": "in_progress",
                "trigger_turn_id": "t1",
                "association_method": "vendor_ids",
            },
            source="assistant",
            turn_id="t1",
            response_id="r1",
        )
        raw_received = await recorder.record_raw(
            RawEvent(
                **context.model_dump(),
                **clock.now().model_dump(),
                direction="received",
                transport="fixture",
                vendor_event_type="fixture.output_audio",
                body={"audio": base64.b64encode(output_pcm).decode()},
            )
        )
        audio_ref = await recorder.store_audio("r1", output_pcm, output_format)
        chunk = new_event(
            context,
            clock,
            event="assistant_audio_chunk",
            source="assistant",
            producer="adapter.fixture",
            response_id="r1",
            turn_id="t1",
            stream_id="r1",
            raw_event_ref=raw_received.raw_event_id,
            timing={"basis": "client_receive"},
            payload={"audio_ref": audio_ref.model_dump(), "chunk_index": 0},
        )
        await emit(
            "assistant_audio_start",
            {"first_chunk_event_id": chunk.event_id, "audio_format": output_format.model_dump()},
            source="assistant",
            response_id="r1",
            turn_id="t1",
        )
        adapter.queue.put_nowait(chunk)
        await recorder.record(await adapter.receive_event())
        await emit(
            "assistant_audio_end",
            {
                "reason": "fixture_end",
                "last_chunk_event_id": chunk.event_id,
                "complete": True,
                "completion_source": "fixture",
            },
            source="assistant",
            response_id="r1",
        )
        await emit(
            "assistant_response_end",
            {"status": "completed", "completion_source": "fixture"},
            source="assistant",
            response_id="r1",
        )
        await adapter.close()
        await emit(
            "session_end",
            {"reason": "fixture_complete", "complete": True, "last_response_ids": ["r1"]},
        )
    recording = read_recording(output / "recording")
    assert recording.audio(audio_ref) == output_pcm
    return {
        "mode": "synthetic_fixture_not_model_benchmark",
        "output": str(output),
        "events": len(recording.events),
        "raw_events": len(recording.raw_events),
        "audio_replay_verified": True,
        "scenario_sha256": scenario.sha256,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run_smoke(args.output)), ensure_ascii=False))


if __name__ == "__main__":
    main()
