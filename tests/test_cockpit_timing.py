"""Seed cockpit timing tolerance leaves the shared benchmark profile unchanged."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from adapters.base import SendReceipt
from benchmark.audio import AudioFormat, AudioRef
from benchmark.config import LatencyProfile
from events.clock import ClockReading
from events.schema import EventDraft, RecordingContext
from scenarios.loader import load_yaml
from scenarios.schema import AudioAsset, PlayAudio
from simulator.input import TimingViolation, send_utterance
from simulator.playback import VirtualPlayback


class ControlledClock:
    def __init__(self):
        self.ns = 1_000_000_000

    def now(self):
        return ClockReading(
            clock_id="controlled",
            timestamp_monotonic_ns=self.ns,
            wall_clock_timestamp=datetime(2026, 9, 22, tzinfo=UTC),
        )


def test_seed_timing_profile_only_relaxes_scheduler_lateness():
    root = Path(__file__).resolve().parents[1]
    strict = LatencyProfile.model_validate(load_yaml(root / "configs/latency.yaml"))
    seed = LatencyProfile.model_validate(
        load_yaml(root / "configs/latency-seed-duplex3-cockpit-tolerant.yaml")
    )
    assert strict.max_send_lateness_ms == strict.max_playback_lateness_ms == 20
    assert seed.max_send_lateness_ms == seed.max_playback_lateness_ms == 150
    assert seed.max_send_duration_ms == strict.max_send_duration_ms == 20
    assert seed.playback_chunk_ms == strict.playback_chunk_ms == 20
    assert seed.tail_silence_ms == strict.tail_silence_ms == 1600


def test_seed_profile_accepts_observed_playback_stall_but_strict_profile_rejects(
    monkeypatch,
):
    root = Path(__file__).resolve().parents[1]
    profiles = (
        LatencyProfile.model_validate(load_yaml(root / "configs/latency.yaml")),
        LatencyProfile.model_validate(
            load_yaml(root / "configs/latency-seed-duplex3-cockpit-tolerant.yaml")
        ),
    )
    clock = ControlledClock()
    original_sleep = asyncio.sleep

    async def stalled_sleep(seconds):
        clock.ns += round(seconds * 1e9) + 120_000_000
        await original_sleep(0)

    monkeypatch.setattr("simulator.playback.asyncio.sleep", stalled_sleep)

    async def play(profile):
        context = RecordingContext(run_id="run", scenario_id="case", attempt_id="a", session_id="s")
        ref = AudioRef(
            **AudioFormat(sample_rate_hz=24000).model_dump(),
            path="audio/out.pcm",
            byte_offset=0,
            byte_length=960,
            sample_offset=0,
            sample_count=480,
        )

        class Sink:
            def audio(self, _):
                return b"\x01\x00" * 480

        events = []
        playback = VirtualPlayback(context, clock, Sink(), events.append, profile)
        playback.submit(
            EventDraft(
                **context.model_dump(),
                **clock.now().model_dump(),
                event="assistant_audio_chunk",
                source="assistant",
                producer="test",
                response_id="r1",
                stream_id="r1",
                timing={"basis": "client_receive"},
                payload={"audio_ref": ref.model_dump(), "chunk_index": 0},
            )
        )
        return playback, events

    async def check():
        strict, _ = await play(profiles[0])
        with pytest.raises(TimingViolation, match="playback_scheduler_late"):
            await strict.finish()
        tolerant, events = await play(profiles[1])
        await tolerant.finish()
        assert [e.event for e in events].count("assistant_playback_chunk") == 1
        assert events[-1].payload.wake_lateness_ns == 120_000_000

    asyncio.run(check())


def test_seed_profile_allows_delayed_first_tail_frame_but_keeps_strict_boundary(
    monkeypatch,
):
    root = Path(__file__).resolve().parents[1]
    strict = LatencyProfile.model_validate(load_yaml(root / "configs/latency.yaml"))
    tolerant = LatencyProfile.model_validate(
        load_yaml(root / "configs/latency-seed-duplex3-cockpit-tolerant.yaml")
    )
    original_sleep = asyncio.sleep
    clock = ControlledClock()
    next_sleep_is_delayed = True

    async def stalled_sleep(seconds):
        nonlocal next_sleep_is_delayed
        clock.ns += round(seconds * 1e9)
        if next_sleep_is_delayed:
            clock.ns += 120_000_000
            next_sleep_is_delayed = False
        await original_sleep(0)

    monkeypatch.setattr("simulator.input.asyncio.sleep", stalled_sleep)
    context = RecordingContext(run_id="run", scenario_id="case", attempt_id="a", session_id="s")
    fmt = AudioFormat(sample_rate_hz=16000)
    asset = AudioAsset(
        path="frozen.wav",
        sha256="0" * 64,
        reference_text="fixture",
        speech_bounds_samples=(0, 320),
        sample_rate_hz=16000,
        provenance={"kind": "synthetic_fixture", "speaker_id": "none"},
    )
    action = PlayAudio(
        action_id="ask",
        type="play_audio",
        asset="frozen",
        turn_id="t1",
        trigger={"type": "session_ready"},
    )

    class Sink:
        async def store_audio(self, stream_id, pcm, format):
            return AudioRef(
                **format.model_dump(),
                path=f"audio/{stream_id}.pcm",
                byte_offset=0,
                byte_length=len(pcm),
                sample_offset=0,
                sample_count=len(pcm) // format.bytes_per_sample_frame,
            )

    class Adapter:
        config = SimpleNamespace(input_audio=fmt, turn_mode="server_vad")

        async def send_audio(self, frame):
            reading = clock.now()
            return SendReceipt(
                stream_id=frame.stream_id,
                chunk_index=frame.chunk_index,
                byte_count=len(frame.pcm),
                started=reading,
                completed=reading,
            )

    async def send(profile):
        observed = []
        await send_utterance(
            Adapter(),
            Sink(),
            context,
            clock,
            observed.append,
            action=action,
            asset=asset,
            pcm=b"\0\0" * 320,
            chunk_ms=20,
            profile=profile.model_copy(update={"tail_silence_ms": 20}),
        )
        return observed

    async def check():
        nonlocal next_sleep_is_delayed
        with pytest.raises(TimingViolation, match="input_deadline_missed_before_send"):
            await send(strict)
        next_sleep_is_delayed = True
        clock.ns = 1_000_000_000
        observed = await send(tolerant)
        chunks = [event for event in observed if event.event == "user_audio_chunk"]
        assert len(chunks) == 2
        assert chunks[-1].payload.silence is True
        assert (chunks[0].payload.send_started_ns - chunks[0].payload.planned_send_ns) == (
            120_000_000
        )

    asyncio.run(check())
