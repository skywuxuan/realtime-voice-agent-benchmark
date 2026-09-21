import json

import pytest
from pydantic import ValidationError

from benchmark.audio import AudioFormat, AudioFrame, AudioRef
from events.bus import BackpressureError, EventBus
from events.clock import elapsed_ms
from events.schema import EventDraft, NormalizedEvent, RawEvent


def test_observation_clock_ignores_wall_jump_and_preserves_negative(clock):
    first = clock.now()
    clock.jump_wall()
    second = clock.now()
    assert second.wall_clock_timestamp < first.wall_clock_timestamp
    assert elapsed_ms(first, second) == 1
    assert elapsed_ms(second, first) == -1
    with pytest.raises(ValueError, match="clock domains"):
        elapsed_ms(first, second.model_copy(update={"clock_id": "other_machine"}))


def test_normalized_roundtrip_keeps_nanosecond_integer_and_payload(event, clock):
    draft = event()
    row = NormalizedEvent(
        **draft.model_dump(), seq=1, recorded_monotonic_ns=clock.now().timestamp_monotonic_ns
    )
    restored = NormalizedEvent.model_validate_json(row.model_dump_json())
    assert restored == row
    assert json.loads(row.model_dump_json())["timestamp_monotonic_ns"] > 2**53
    assert restored.payload.detector == "fixture"


@pytest.mark.parametrize(
    "update",
    [
        {"schema_version": "1.0"},
        {"event": "invented_vendor_event"},
        {"payload": {}},
        {"payload": {"detector": "x", "unexpected": True}},
        {"timestamp_monotonic_ns": True},
        {"wall_clock_timestamp": "2026-09-17T00:00:00"},
    ],
)
def test_invalid_event_contract_rejected(event, update):
    data = event().model_dump()
    data.update(update)
    with pytest.raises(ValidationError):
        EventDraft.model_validate(data)


def test_record_timestamp_cannot_precede_observation(event):
    draft = event()
    with pytest.raises(ValidationError, match="record time"):
        NormalizedEvent(
            **draft.model_dump(), seq=1, recorded_monotonic_ns=draft.timestamp_monotonic_ns - 1
        )


def test_audio_reference_cannot_silently_change_time_or_path():
    fields = dict(
        path="audio/r1.pcm",
        sample_rate_hz=24000,
        byte_offset=0,
        byte_length=960,
        sample_offset=0,
        sample_count=480,
    )
    AudioRef(**fields)
    for update in (
        {"sample_count": 479},
        {"byte_offset": 2},
        {"path": "../outside.pcm"},
        {"path": "/absolute.pcm"},
    ):
        with pytest.raises(ValidationError):
            AudioRef(**{**fields, **update})
    with pytest.raises(ValidationError, match="whole"):
        AudioFrame(
            pcm=b"\x00",
            format=AudioFormat(sample_rate_hz=16000),
            stream_id="s",
            turn_id="t",
            chunk_index=0,
            sample_offset=0,
        )


def test_concurrent_tool_correlations_cannot_be_mixed(event):
    with pytest.raises(ValidationError, match="call_id differs"):
        event(
            "tool_call_start",
            {"name": "weather", "call_id": "call_b", "response_id": "r1"},
            call_id="call_a",
            response_id="r1",
        )
    with pytest.raises(ValidationError, match="requires call_id"):
        event(
            "tool_call_end",
            {"name": "weather", "arguments": {}, "valid_json": True, "completion_source": "done"},
        )


def test_unknown_vendor_events_remain_raw(context, clock):
    raw = RawEvent(
        **context.model_dump(),
        **clock.now().model_dump(),
        direction="received",
        transport="fixture",
        vendor_event_type="vendor.future.type",
        body={"nested": [1, "新字段"]},
    )
    assert RawEvent.model_validate_json(raw.model_dump_json()) == raw


def test_backpressure_never_partially_delivers(event, clock):
    bus = EventBus()
    fast, slow = bus.subscribe("fast", capacity=2), bus.subscribe("slow", capacity=1)
    draft = event()
    row = NormalizedEvent(
        **draft.model_dump(), seq=1, recorded_monotonic_ns=clock.now().timestamp_monotonic_ns
    )
    bus.publish(row)
    assert fast.get_nowait() == row
    with pytest.raises(BackpressureError):
        bus.publish(row)
    assert fast.empty() and slow.qsize() == 1
    assert bus.high_watermarks == {"fast": 1, "slow": 1}
