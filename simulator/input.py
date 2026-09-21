"""Audio sender with absolute deadlines and explicit annotated speech boundaries."""

import asyncio
from collections.abc import Callable

from adapters.base import RealtimeModelAdapter
from benchmark.audio import AudioFrame, AudioRef
from benchmark.config import LatencyProfile
from events.buffered import BufferedArtifacts
from events.clock import Clock
from events.schema import EventDraft, RecordingContext
from scenarios.schema import AudioAsset, PlayAudio


class TimingViolation(RuntimeError):
    pass


async def send_utterance(
    adapter: RealtimeModelAdapter,
    sink: BufferedArtifacts,
    context: RecordingContext,
    clock: Clock,
    publish: Callable[[EventDraft], None],
    *,
    action: PlayAudio,
    asset: AudioAsset,
    pcm: bytes,
    chunk_ms: int,
    profile: LatencyProfile,
    stop_tail: asyncio.Event | None = None,
) -> None:
    format = adapter.config.input_audio
    if asset.sample_rate_hz != format.sample_rate_hz:
        raise ValueError(
            "input asset rate differs from configured rate; preprocess and freeze it first"
        )
    width, rate = format.bytes_per_sample_frame, format.sample_rate_hz
    samples = len(pcm) // width
    if asset.speech_bounds_samples[1] > samples or any(
        r.bounds_samples[1] > samples for r in asset.regions
    ):
        raise ValueError("annotated input exceeds WAV length")
    tail = (
        b"\0" * (rate * profile.tail_silence_ms // 1000 * width)
        if adapter.config.turn_mode == "server_vad"
        else b""
    )
    source = pcm + tail
    ref = await sink.store_audio("input", source, format)
    speech_start, speech_end = asset.speech_bounds_samples
    points = {0, len(source) // width, samples, speech_start, speech_end}
    for region in asset.regions:
        points.update(region.bounds_samples)
    points.update(range(0, len(source) // width, rate * chunk_ms // 1000))
    boundaries = sorted(points)
    origin = clock.now()

    def event(kind, payload, reading):
        return EventDraft(
            **context.model_dump(),
            **reading.model_dump(),
            event=kind,
            source="user",
            producer="simulator.input",
            timing={"basis": "client_send"},
            turn_id=action.turn_id,
            stream_id="input",
            payload=payload,
        )

    def boundary(kind, sample, reading, cause=None):
        payload = {
            "action_id": action.action_id,
            "annotation_source": asset.boundary_annotation.method,
        }
        if kind == "user_audio_start":
            payload.update(asset_id=action.asset, sample_index=sample)
        else:
            payload["end_sample"] = sample
        draft = event(kind, payload, reading)
        if cause:
            draft = draft.model_copy(update={"causal_event_id": cause})
        publish(draft)

    def region_boundaries(sample, reading, cause=None):
        for region in asset.regions:
            for kind, boundary_sample in zip(
                ("input_region_start", "input_region_end"), region.bounds_samples
            ):
                if boundary_sample == sample:
                    draft = event(
                        kind,
                        {
                            "action_id": action.action_id,
                            "region_id": region.region_id,
                            "kind": region.kind,
                            "sample_index": sample,
                        },
                        reading,
                    )
                    if cause:
                        draft = draft.model_copy(update={"causal_event_id": cause})
                    publish(draft)

    region_boundaries(0, origin)
    if speech_start == 0:
        boundary("user_audio_start", 0, origin)
    for index, (start, end) in enumerate(zip(boundaries, boundaries[1:])):
        if start >= samples and stop_tail is not None and stop_tail.is_set():
            break
        planned = origin.timestamp_monotonic_ns + end * 1_000_000_000 // rate
        await asyncio.sleep(max(0, (planned - clock.now().timestamp_monotonic_ns) / 1e9))
        if start >= samples and stop_tail is not None and stop_tail.is_set():
            break
        if clock.now().timestamp_monotonic_ns - planned > profile.max_send_lateness_ms * 1e6:
            raise TimingViolation("input_deadline_missed_before_send")
        chunk = source[start * width : end * width]
        receipt = await adapter.send_audio(
            AudioFrame(
                pcm=chunk,
                format=format,
                stream_id="input",
                turn_id=action.turn_id,
                chunk_index=index,
                sample_offset=start,
            )
        )
        chunk_ref = AudioRef(
            **format.model_dump(),
            path=ref.path,
            byte_offset=ref.byte_offset + start * width,
            byte_length=len(chunk),
            sample_offset=ref.sample_offset + start,
            sample_count=end - start,
        )
        sent_event = event(
            "user_audio_chunk",
            {
                "audio_ref": chunk_ref.model_dump(),
                "chunk_index": index,
                "planned_send_ns": planned,
                "send_started_ns": receipt.started.timestamp_monotonic_ns,
                "send_completed_ns": receipt.completed.timestamp_monotonic_ns,
                "silence": start >= speech_end
                or end <= speech_start
                or any(
                    region.kind == "pause"
                    and region.bounds_samples[0] <= start
                    and end <= region.bounds_samples[1]
                    for region in asset.regions
                ),
            },
            receipt.completed,
        )
        publish(sent_event)
        region_boundaries(end, receipt.completed, sent_event.event_id)
        if end == speech_start:
            boundary("user_audio_start", end, receipt.completed, sent_event.event_id)
        if end == speech_end:
            boundary("user_audio_end", end, receipt.completed, sent_event.event_id)
        lateness = (receipt.started.timestamp_monotonic_ns - planned) / 1e6
        duration = (
            receipt.completed.timestamp_monotonic_ns - receipt.started.timestamp_monotonic_ns
        ) / 1e6
        if lateness > profile.max_send_lateness_ms or duration > profile.max_send_duration_ms:
            raise TimingViolation("input_send_timing_out_of_bounds")
    if adapter.config.turn_mode == "manual":
        publish(event("user_turn_commit", {"phase": "requested"}, clock.now()))
        await adapter.commit_turn(action.turn_id)
        publish(event("user_turn_commit", {"phase": "submitted"}, clock.now()))
