import asyncio

import pytest

from benchmark.audio import AudioFormat
from events.buffered import ArtifactBackpressure, BufferedArtifacts
from events.recorder import EventRecorder
from events.replay import read_recording
from events.schema import RawEvent


def test_blocked_disk_does_not_block_audio_or_raw_submission(tmp_path, context, clock, event):
    async def run():
        recorder = EventRecorder(tmp_path / "case", context, clock=clock)
        await recorder.__aenter__()
        entered, release = asyncio.Event(), asyncio.Event()
        original = recorder.record_raw

        async def slow_raw(raw):
            entered.set()
            await release.wait()
            return await original(raw)

        recorder.record_raw = slow_raw
        sink = BufferedArtifacts(recorder)
        sink.emit(event("session_start"))
        raw = await sink.record_raw(
            RawEvent(
                **context.model_dump(),
                **clock.now().model_dump(),
                direction="received",
                transport="fixture",
                body={"type": "audio"},
            )
        )
        await entered.wait()
        ref = await asyncio.wait_for(
            sink.store_audio("r1", b"\1\0" * 480, AudioFormat(sample_rate_hz=24000)), 0.1
        )
        assert sink.audio(ref) == b"\1\0" * 480
        assert not (tmp_path / "case" / ref.path).exists()
        draft = event(
            "assistant_audio_chunk",
            {"audio_ref": ref.model_dump(), "chunk_index": 0},
            response_id="r1",
            stream_id="r1",
            raw_event_ref=raw.raw_event_id,
        )
        sink.emit(draft)
        sink.emit(event("session_end"))
        finishing = asyncio.create_task(sink.finish())
        await asyncio.sleep(0)
        finishing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await finishing
        release.set()
        await sink.finish()
        await recorder.close()
        rec = read_recording(tmp_path / "case")
        assert rec.audio(ref) == b"\1\0" * 480
        assert rec.events[1].timestamp_monotonic_ns == draft.timestamp_monotonic_ns
        assert rec.events[1].raw_event_ref == raw.raw_event_id

    asyncio.run(run())


def test_bounded_queue_rejects_overflow_without_silent_loss(tmp_path, context, clock, event):
    async def run():
        recorder = EventRecorder(tmp_path / "case", context, clock=clock)
        await recorder.__aenter__()
        sink = BufferedArtifacts(recorder, capacity=1)
        sink.emit(event("session_start"))
        with pytest.raises(ArtifactBackpressure):
            sink.emit(event())
        await sink.finish()
        await recorder.close(complete=False)
        rec = read_recording(tmp_path / "case", allow_partial=True)
        assert len(rec.events) == 1
        assert sink.pending_bytes == 0

    asyncio.run(run())


def test_writer_failure_is_propagated_and_queue_drains(tmp_path, context, clock):
    async def run():
        recorder = EventRecorder(tmp_path / "case", context, clock=clock)
        await recorder.__aenter__()

        async def fail(*args):
            raise OSError("simulated disk full")

        recorder.store_audio = fail
        sink = BufferedArtifacts(recorder)
        await sink.store_audio("r", b"\0\0", AudioFormat(sample_rate_hz=16000))
        with pytest.raises(ArtifactBackpressure, match="writer failed"):
            await asyncio.wait_for(sink.finish(), 1)
        assert sink.pending_bytes == 0
        await recorder.close(complete=False)

    asyncio.run(run())
