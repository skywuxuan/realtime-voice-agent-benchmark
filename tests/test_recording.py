import asyncio
import json
import threading

import pytest

from benchmark.audio import AudioFormat
from benchmark.contracts import canonical_json, content_hash, pretty_json
from events.recorder import EventRecorder
from events.replay import RecordingError, read_recording
from events.schema import RawEvent


def test_record_replay_audio_raw_ids_and_observation_time(tmp_path, context, clock, event):
    async def run():
        async with EventRecorder(
            tmp_path / "case", context, clock=clock, config={"prompt": "中文提示词"}
        ) as recorder:
            await recorder.record(event("session_start"))
            raw = await recorder.record_raw(
                RawEvent(
                    **context.model_dump(),
                    **clock.now().model_dump(),
                    direction="received",
                    transport="fixture",
                    body={"type": "future.vendor.event", "keep_me": {"a": 1}},
                )
            )
            fmt = AudioFormat(sample_rate_hz=24000)
            first = await recorder.store_audio("r1", b"\x01\x02" * 480, fmt)
            second = await recorder.store_audio("r1", b"\x03\x04" * 240, fmt)
            draft = event(
                "assistant_audio_chunk",
                {"audio_ref": second.model_dump(), "chunk_index": 1, "late_after_cancel": True},
                response_id="r1",
                stream_id="r1",
                raw_event_ref=raw.raw_event_id,
            )
            clock.ns += 5_000_000
            clock.jump_wall()
            saved = await recorder.record(draft)
            assert saved.timestamp_monotonic_ns == draft.timestamp_monotonic_ns
            assert saved.recorded_monotonic_ns > saved.timestamp_monotonic_ns
            await recorder.record(event("session_end"))
        recording = read_recording(tmp_path / "case")
        assert recording.audio(first) == b"\x01\x02" * 480
        assert recording.audio(second) == b"\x03\x04" * 240
        assert second.sample_offset == 480 and second.byte_offset == 960
        assert recording.events[1].payload.late_after_cancel is True
        assert recording.raw_events[0].body["keep_me"] == {"a": 1}
        assert read_recording(tmp_path / "case").events == recording.events
        assert (tmp_path / "case" / "config.json").read_text().startswith("{\n  ")
        assert (tmp_path / "case" / "manifest.json").read_text().startswith("{\n  ")
        events = (tmp_path / "case" / "events.jsonl").read_text().splitlines()
        assert len(events) == len(recording.events)
        assert all(line.startswith("{") and not line.startswith("{ ") for line in events)

    asyncio.run(run())


def test_pretty_json_is_readable_without_changing_content_hash():
    data = {"prompt": "中文提示词", "tools": [{"name": "setVolume", "value": 40}]}
    display = pretty_json(data)
    assert display.startswith("{\n  ") and display.endswith("\n")
    assert "中文提示词" in display and "\\u4e2d" not in display
    assert "\n" not in canonical_json(data)
    assert json.loads(display) == data
    assert content_hash(json.loads(display)) == content_hash(data)


def test_concurrent_writers_have_gap_free_sequences(tmp_path, context, clock, event):
    async def run():
        async with EventRecorder(tmp_path / "case", context, clock=clock) as recorder:
            await recorder.record(event("session_start"))
            await asyncio.gather(*(recorder.record(event()) for _ in range(20)))
            await recorder.record(event("session_end"))
        assert [e.seq for e in read_recording(tmp_path / "case").events] == list(range(1, 23))

    asyncio.run(run())


def test_redaction_covers_raw_config_and_error_text(tmp_path, context, clock, event):
    secret = "test-sensitive-value-never-persist"

    async def run():
        async with EventRecorder(
            tmp_path / "case",
            context,
            clock=clock,
            secrets=(secret,),
            config={
                "DASHSCOPE_API_KEY": secret,
                "endpoint": "wss://example.invalid/ws?api_key=another-value",
            },
        ) as recorder:
            await recorder.record(event("session_start"))
            raw = await recorder.record_raw(
                RawEvent(
                    **context.model_dump(),
                    **clock.now().model_dump(),
                    direction="sent",
                    transport="fixture",
                    body={
                        "headers": {"Authorization": "Bearer other-secret"},
                        "nested": [{"api_key": secret}],
                        "message": f"failed with {secret}",
                        "audio": "AQID",
                    },
                )
            )
            assert raw.redacted_fields
            await recorder.record(
                event(
                    "error",
                    {
                        "category": "fixture",
                        "code": "bad",
                        "message_redacted": f"failure {secret}",
                        "fatal": False,
                        "scope": "session",
                        "retryable": False,
                    },
                )
            )
            await recorder.record(event("session_end"))
        for path in (tmp_path / "case").glob("*.json*"):
            contents = path.read_text()
            assert (
                secret not in contents
                and "other-secret" not in contents
                and "another-value" not in contents
            )
        assert read_recording(tmp_path / "case").raw_events[0].body["audio"] == "AQID"

    asyncio.run(run())


def test_duplicate_missing_raw_and_changed_format_rejected(tmp_path, context, clock, event):
    async def run():
        async with EventRecorder(tmp_path / "case", context, clock=clock) as recorder:
            first = event("session_start")
            await recorder.record(first)
            with pytest.raises(RecordingError, match="duplicate"):
                await recorder.record(first)
            with pytest.raises(RecordingError, match="raw event"):
                await recorder.record(event(raw_event_ref="missing"))
            await recorder.store_audio("r1", b"\0\0", AudioFormat(sample_rate_hz=24000))
            with pytest.raises(RecordingError, match="format"):
                await recorder.store_audio("r1", b"\0\0", AudioFormat(sample_rate_hz=16000))
            await recorder.record(event("session_end"))
        assert len(read_recording(tmp_path / "case").events) == 2

    asyncio.run(run())


def test_exception_preserves_partial_but_default_replay_refuses(tmp_path, context, clock, event):
    async def run():
        with pytest.raises(RuntimeError, match="transport failed"):
            async with EventRecorder(tmp_path / "case", context, clock=clock) as recorder:
                await recorder.record(event("session_start"))
                raise RuntimeError("transport failed")
        with pytest.raises(RecordingError, match="incomplete"):
            read_recording(tmp_path / "case")
        assert len(read_recording(tmp_path / "case", allow_partial=True).events) == 1

    asyncio.run(run())


def test_unresolved_forward_reference_cannot_finalize_complete(tmp_path, context, clock, event):
    async def run():
        with pytest.raises(RecordingError, match="missing normalized"):
            async with EventRecorder(tmp_path / "case", context, clock=clock) as recorder:
                await recorder.record(event("session_start"))
                await recorder.record(
                    event(
                        "assistant_audio_start",
                        {
                            "first_chunk_event_id": "never_recorded",
                            "audio_format": {"sample_rate_hz": 24000},
                        },
                        response_id="r1",
                    )
                )
                await recorder.record(event("session_end"))
        assert (
            read_recording(tmp_path / "case", allow_partial=True).manifest["status"] == "incomplete"
        )

    asyncio.run(run())


def test_tampered_artifact_and_overwrite_rejected(tmp_path, context, clock, event):
    async def run():
        async with EventRecorder(tmp_path / "case", context, clock=clock) as recorder:
            await recorder.record(event("session_start"))
            await recorder.record(event("session_end"))
        with pytest.raises(FileExistsError):
            async with EventRecorder(tmp_path / "case", context, clock=clock):
                pass
        path = tmp_path / "case" / "events.jsonl"
        path.write_bytes(path.read_bytes() + b"\n")
        with pytest.raises(RecordingError, match="integrity"):
            read_recording(tmp_path / "case")

    asyncio.run(run())


def test_crash_with_truncated_last_line_allows_only_explicit_prefix_inspection(
    tmp_path, context, clock, event
):
    async def run():
        recorder = EventRecorder(tmp_path / "case", context, clock=clock)
        await recorder.__aenter__()
        await recorder.record(event("session_start"))
        # Emulate process death: no close/final manifest, partial final disk write.
        with (tmp_path / "case" / "events.jsonl").open("ab") as stream:
            stream.write(b'{"schema_version":')
        with pytest.raises(RecordingError, match="incomplete"):
            read_recording(tmp_path / "case")
        partial = read_recording(tmp_path / "case", allow_partial=True)
        assert len(partial.events) == 1 and partial.manifest["status"] == "recording"

    asyncio.run(run())


def test_cancellation_waits_for_inflight_write_then_preserves_it(tmp_path, context, clock, event):
    async def run():
        recorder = EventRecorder(tmp_path / "case", context, clock=clock)
        await recorder.__aenter__()
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        original = recorder._append

        def blocked_append(*args):
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(timeout=5)
            original(*args)

        recorder._append = blocked_append
        task = asyncio.create_task(recorder.record(event("session_start")))
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await recorder.close(complete=False, reason="cancelled")
        partial = read_recording(tmp_path / "case", allow_partial=True)
        assert partial.manifest["event_count"] == len(partial.events) == 1

    asyncio.run(run())


def test_blob_hash_and_context_validation(tmp_path, context, clock, event):
    async def run():
        async with EventRecorder(tmp_path / "case", context, clock=clock) as recorder:
            await recorder.record(event("session_start"))
            blob = await recorder.store_blob(b"\x01\x02")
            await recorder.record_raw(
                RawEvent(
                    **context.model_dump(),
                    **clock.now().model_dump(),
                    direction="received",
                    transport="fixture",
                    body_ref=blob,
                )
            )
            wrong = event().model_copy(update={"session_id": "another_session"})
            with pytest.raises(RecordingError, match="context"):
                await recorder.record(wrong)
            await recorder.record(event("session_end"))
        assert read_recording(tmp_path / "case").raw_events[0].body_ref == blob
        manifest = json.loads((tmp_path / "case" / "manifest.json").read_text())
        assert blob.path in manifest["files"]

    asyncio.run(run())


def test_audio_anchor_cannot_cross_response_boundary(tmp_path, context, clock, event):
    async def run():
        with pytest.raises(RecordingError, match="different response"):
            async with EventRecorder(tmp_path / "case", context, clock=clock) as recorder:
                await recorder.record(event("session_start"))
                fmt = AudioFormat(sample_rate_hz=24000)
                reference = await recorder.store_audio("response_b", b"\0\0", fmt)
                chunk = event(
                    "assistant_audio_chunk",
                    {
                        "audio_ref": reference.model_dump(),
                        "chunk_index": 0,
                    },
                    response_id="response_b",
                    stream_id="response_b",
                )
                await recorder.record(chunk)
                await recorder.record(
                    event(
                        "assistant_audio_start",
                        {
                            "first_chunk_event_id": chunk.event_id,
                            "audio_format": fmt.model_dump(),
                        },
                        response_id="response_a",
                    )
                )
                await recorder.record(event("session_end"))
        assert (
            read_recording(tmp_path / "case", allow_partial=True).manifest["status"] == "incomplete"
        )

    asyncio.run(run())
