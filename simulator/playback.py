"""Paced virtual playback; every received sample is played or explicitly dropped."""

import asyncio
from collections.abc import Callable

from benchmark.audio import AudioRef
from benchmark.config import LatencyProfile
from events.buffered import BufferedArtifacts
from events.clock import Clock
from events.schema import EventDraft, RecordingContext
from simulator.input import TimingViolation


def audio_part(ref: AudioRef, offset: int, count: int) -> AudioRef:
    width = ref.channels * 2
    return AudioRef(
        **ref.model_dump(exclude={"byte_offset", "byte_length", "sample_offset", "sample_count"}),
        byte_offset=ref.byte_offset + offset * width,
        byte_length=count * width,
        sample_offset=ref.sample_offset + offset,
        sample_count=count,
    )


class VirtualPlayback:
    def __init__(
        self,
        context: RecordingContext,
        clock: Clock,
        sink: BufferedArtifacts,
        publish: Callable[[EventDraft], None],
        profile: LatencyProfile,
    ):
        self.context, self.clock, self.sink, self.publish = context, clock, sink, publish
        self.profile = profile
        self.queue: asyncio.Queue = asyncio.Queue(1024)
        self.segments: list[tuple[int, bytes]] = []
        self.started: set[str] = set()
        self._formats: dict[str, tuple[int, int]] = {}
        self._cancelled: dict[str, EventDraft] = {}
        self._originals: dict[str, EventDraft] = {}
        self._remaining: dict[str, int] = {}
        self._quantum: tuple[str, int, int] | None = None
        self._stopped: set[str] = set()
        self._closed = False
        self._aborting = False
        self._failure: Exception | None = None
        self._finish_task: asyncio.Task | None = None
        self.max_lateness_ns = 0
        self._task = asyncio.create_task(self._play())

    def remaining_ms(self, response_id: str) -> float:
        """Received but unplayed PCM, including the unfinished current quantum."""
        if response_id in self._stopped:
            return 0.0
        rate, _ = self._formats.get(response_id, (1, 0))
        value = self._remaining.get(response_id, 0) * 1000 / rate
        if self._quantum and self._quantum[0] == response_id:
            _, start, end = self._quantum
            value -= max(0, min(self.clock.now().timestamp_monotonic_ns, end) - start) / 1e6
        return max(0.0, value)

    def submit(self, event: EventDraft) -> None:
        if event.event not in {
            "assistant_audio_chunk",
            "assistant_audio_end",
            "assistant_cancelled",
        }:
            return
        if self._closed or self._failure:
            raise RuntimeError("playback is not accepting events")
        rid = event.response_id
        if event.event == "assistant_cancelled":
            self._cancelled.setdefault(rid, event)
            # Wake an idle player; an active quantum finishes before the stop.
            self.queue.put_nowait(event)
        elif event.event == "assistant_audio_chunk":
            ref = event.payload.audio_ref
            self._formats.setdefault(rid, (ref.sample_rate_hz, ref.sample_offset))
            self._originals[rid] = event
            self._remaining[rid] = self._remaining.get(rid, 0) + ref.sample_count
            if rid in self._cancelled or rid in self._stopped:
                self._drop(event, 0, "audio_after_stop")
            else:
                self.queue.put_nowait(event)
        else:
            self.queue.put_nowait(event)

    def _stop(self, original: EventDraft, reason: str) -> None:
        rid = original.response_id
        if rid in self._stopped:
            return
        self._stopped.add(rid)
        if rid not in self.started:
            return  # No audible stop before the first played sample.
        rate, cursor = self._formats[rid]
        self._emit(
            "assistant_playback_stop",
            original,
            self.clock.now(),
            sample_offset=cursor,
            sample_count=0,
            sample_rate_hz=rate,
            stop_reason=reason,
        )

    def _drop(self, event: EventDraft, offset: int, reason: str) -> None:
        ref = event.payload.audio_ref
        count = ref.sample_count - offset
        if count <= 0:
            return
        part = audio_part(ref, offset, count)
        self._remaining[event.response_id] -= count
        self._emit(
            "audio_chunk_dropped",
            event,
            self.clock.now(),
            audio_ref=part.model_dump(),
            reason=reason,
            chunk_event_id=event.event_id,
        )

    def _emit(self, kind, original, reading, **payload):
        self.publish(
            EventDraft(
                **self.context.model_dump(),
                **reading.model_dump(),
                event=kind,
                source="assistant",
                producer="simulator.playback",
                timing={"basis": "virtual_playback"},
                response_id=original.response_id,
                turn_id=original.turn_id,
                stream_id=original.stream_id,
                causal_event_id=original.event_id,
                payload=payload,
            )
        )

    async def _chunk(self, event):
        ref, rid = event.payload.audio_ref, event.response_id
        pcm = self.sink.audio(ref)
        width = ref.channels * 2
        quantum = max(1, ref.sample_rate_hz * self.profile.playback_chunk_ms // 1000)
        offset = 0
        try:
            while offset < ref.sample_count:
                if self._aborting or rid in self._cancelled or rid in self._stopped:
                    reason = "case_abort" if self._aborting else "assistant_cancelled"
                    self._stop(self._cancelled.get(rid, event), reason)
                    self._drop(event, offset, reason)
                    return
                count = min(quantum, ref.sample_count - offset)
                reading = self.clock.now()
                if rid not in self.started:
                    self.started.add(rid)
                    self._emit(
                        "assistant_playback_start",
                        event,
                        reading,
                        sample_offset=ref.sample_offset + offset,
                        sample_count=0,
                        sample_rate_hz=ref.sample_rate_hz,
                    )
                end_ns = (
                    reading.timestamp_monotonic_ns + count * 1_000_000_000 // ref.sample_rate_hz
                )
                self._quantum = (rid, reading.timestamp_monotonic_ns, end_ns)
                await asyncio.sleep(
                    max(0, (end_ns - self.clock.now().timestamp_monotonic_ns) / 1e9)
                )
                late = max(0, self.clock.now().timestamp_monotonic_ns - end_ns)
                self.max_lateness_ns = max(self.max_lateness_ns, late)
                part = audio_part(ref, offset, count)
                self.segments.append(
                    (reading.timestamp_monotonic_ns, pcm[offset * width : (offset + count) * width])
                )
                self._formats[rid] = (ref.sample_rate_hz, part.sample_offset + count)
                self._remaining[rid] -= count
                self._quantum = None
                self._emit(
                    "assistant_playback_chunk",
                    event,
                    reading,
                    sample_offset=part.sample_offset,
                    sample_count=count,
                    sample_rate_hz=ref.sample_rate_hz,
                    audio_ref=part.model_dump(),
                    planned_end_ns=end_ns,
                    wake_lateness_ns=late,
                )
                offset += count
                if late > self.profile.max_playback_lateness_ms * 1e6:
                    raise TimingViolation("playback_scheduler_late")
            if rid in self._cancelled:
                self._stop(self._cancelled[rid], "assistant_cancelled")
            elif self._aborting:
                self._stop(event, "case_abort")
        except Exception:
            self._drop(event, offset, "playback_failed")
            raise
        finally:
            self._quantum = None

    async def _play(self):
        try:
            while True:
                event = await self.queue.get()
                try:
                    if event is None:
                        return
                    rid = event.response_id
                    if event.event == "assistant_audio_chunk":
                        await self._chunk(event)
                    elif rid in self._cancelled:
                        self._stop(self._cancelled[rid], "assistant_cancelled")
                    elif event.event == "assistant_audio_end":
                        self._stop(event, "case_abort" if self._aborting else event.payload.reason)
                finally:
                    self.queue.task_done()
        except Exception as error:
            self._failure = error
            while not self.queue.empty():
                pending = self.queue.get_nowait()
                if pending is not None and pending.event == "assistant_audio_chunk":
                    self._drop(pending, 0, "playback_failed")
                self.queue.task_done()

    async def finish(self) -> None:
        if self._finish_task is None:
            self._finish_task = asyncio.create_task(self._finish())
        await asyncio.shield(self._finish_task)
        if self._failure:
            raise self._failure

    async def _finish(self) -> None:
        self._closed = True
        await self.queue.join()
        if not self._task.done():
            await self.queue.put(None)
        await self._task

    async def abort(self) -> None:
        """Finish the current quantum, drop queued samples and mark case_abort stops."""
        self._aborting = True
        await self.finish()
        for rid in self.started - self._stopped:
            self._stop(self._originals[rid], "case_abort")
