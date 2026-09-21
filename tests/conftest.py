import hashlib
import wave
from datetime import UTC, datetime, timedelta

import pytest

from events.clock import ClockReading
from events.schema import RecordingContext, new_event


class ManualClock:
    def __init__(self):
        self.ns = 10_000_000_000_000_000
        self.wall = datetime(2026, 9, 17, tzinfo=UTC)

    def now(self):
        self.ns += 1_000_000
        return ClockReading(
            clock_id="test_clock", timestamp_monotonic_ns=self.ns, wall_clock_timestamp=self.wall
        )

    def jump_wall(self):
        self.wall -= timedelta(days=1)


@pytest.fixture
def clock():
    return ManualClock()


@pytest.fixture
def context():
    return RecordingContext(
        run_id="run_test", scenario_id="case_test", attempt_id="a1", session_id="s1"
    )


@pytest.fixture
def event(context, clock):
    def make(kind="vad_start", payload=None, **fields):
        defaults = {
            "vad_start": {"detector": "fixture"},
            "session_start": {
                "vendor_session_id": None,
                "adapter_version": "fixture",
                "capabilities": {},
            },
            "session_end": {"reason": "complete", "complete": True},
        }
        return new_event(
            context,
            clock,
            event=kind,
            payload=payload if payload is not None else defaults[kind],
            source="system",
            producer="test",
            timing={"basis": "client_receive"},
            **fields,
        )

    return make


@pytest.fixture
def scenario_data(tmp_path):
    path = tmp_path / "input.wav"
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x01\x00" * 1600)
    return {
        "schema_version": "0.1",
        "scenario_id": "latency_001",
        "scenario_version": 1,
        "suite": "realtime",
        "category": "latency",
        "seed": 17,
        "world": {"now": "2026-09-19T10:00:00+08:00", "timezone": "Asia/Shanghai"},
        "capabilities_required": ["audio_input", "audio_output"],
        "session": {"system_prompt": "请用中文回答。", "turn_mode": "manual"},
        "audio": {
            "assets": {
                "question": {
                    "path": "input.wav",
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "reference_text": "secret oracle text",
                    "speech_bounds_samples": [0, 1600],
                    "sample_rate_hz": 16000,
                    "provenance": {"kind": "synthetic_fixture", "speaker_id": "none"},
                }
            }
        },
        "actions": [
            {
                "action_id": "ask",
                "type": "play_audio",
                "asset": "question",
                "turn_id": "t1",
                "trigger": {"type": "session_ready"},
            }
        ],
        "oracle": {
            "stimulus": "utterance",
            "metric_profile": "test",
            "expected_new_intent": {"city": "secret city"},
            "assertions": [{"type": "audio_received"}],
        },
        "termination": {},
    }
