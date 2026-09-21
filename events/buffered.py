"""Bounded write-behind artifacts: reserve references without waiting for disk IO."""

import asyncio

from benchmark.audio import AudioFormat, AudioRef
from events.recorder import EventRecorder, audio_stream_path
from events.schema import EventDraft, RawEvent


class ArtifactBackpressure(RuntimeError):
    pass


class BufferedArtifacts:
    def __init__(
        self, recorder: EventRecorder, *, capacity: int = 4096, max_bytes: int = 32 * 1024 * 1024
    ):
        if capacity < 1 or max_bytes < 1:
            raise ValueError("artifact queue bounds must be positive")
        self.recorder = recorder
        self.queue: asyncio.Queue = asyncio.Queue(capacity)
        self.max_bytes = max_bytes
        self.pending_bytes = 0
        self.audio_bytes = 0
        self.high_watermark = 0
        self.high_watermark_bytes = 0
        self._streams: dict[str, tuple[AudioFormat, int]] = {}
        self._audio: dict[tuple[str, int, int], bytes] = {}
        self._failure: Exception | None = None
        self._closed = False
        self._finish_task: asyncio.Task | None = None
        self._worker = asyncio.create_task(self._write_loop())

    def _submit(self, kind: str, args: tuple, size: int) -> None:
        if self._closed:
            raise RuntimeError("artifact queue is closed")
        if self._failure:
            raise ArtifactBackpressure("artifact writer failed") from self._failure
        if self.queue.full() or self.pending_bytes + size > self.max_bytes:
            raise ArtifactBackpressure("artifact queue capacity exceeded; run is invalid")
        self.queue.put_nowait((kind, args, size))
        self.pending_bytes += size
        self.high_watermark = max(self.high_watermark, self.queue.qsize())
        self.high_watermark_bytes = max(self.high_watermark_bytes, self.pending_bytes)

    async def record_raw(self, raw: RawEvent) -> RawEvent:
        snapshot = raw.model_copy(deep=True)
        self._submit("raw", (snapshot,), len(snapshot.model_dump_json().encode()))
        return snapshot

    async def store_audio(self, stream_id: str, pcm: bytes, format: AudioFormat) -> AudioRef:
        pcm = bytes(pcm)
        if not pcm or len(pcm) % format.bytes_per_sample_frame:
            raise ValueError("audio must contain whole, nonempty PCM sample frames")
        old_format, offset = self._streams.get(stream_id, (format, 0))
        if old_format != format:
            raise ValueError("stream audio format changed")
        if self.audio_bytes + len(pcm) > self.max_bytes:
            raise ArtifactBackpressure("in-memory PCM bound exceeded; run is invalid")
        ref = AudioRef(
            **format.model_dump(),
            path=audio_stream_path(stream_id),
            byte_offset=offset,
            byte_length=len(pcm),
            sample_offset=offset // format.bytes_per_sample_frame,
            sample_count=len(pcm) // format.bytes_per_sample_frame,
        )
        self._submit("audio", (stream_id, pcm, format, ref), len(pcm))
        self._streams[stream_id] = (format, offset + len(pcm))
        self._audio[(ref.path, ref.byte_offset, ref.byte_length)] = pcm
        self.audio_bytes += len(pcm)
        return ref

    def audio(self, ref: AudioRef) -> bytes:
        return self._audio[(ref.path, ref.byte_offset, ref.byte_length)]

    def audio_part(self, ref: AudioRef) -> bytes:
        for (path, offset, length), pcm in self._audio.items():
            if (
                path == ref.path
                and offset <= ref.byte_offset
                and ref.byte_offset + ref.byte_length <= offset + length
            ):
                start = ref.byte_offset - offset
                return pcm[start : start + ref.byte_length]
        raise ValueError("audio reference was not retained in memory")

    def emit(self, event: EventDraft) -> None:
        snapshot = event.model_copy(deep=True)
        reading = self.recorder.clock.now()
        self._submit("event", (snapshot, reading), len(snapshot.model_dump_json().encode()))

    async def _write_loop(self):
        while True:
            job = await self.queue.get()
            if job is None:
                self.queue.task_done()
                return
            kind, args, size = job
            try:
                if self._failure:
                    continue
                if kind == "raw":
                    await self.recorder.record_raw(*args)
                elif kind == "event":
                    await self.recorder.record(args[0], recorded_at=args[1])
                else:
                    stream_id, pcm, format, expected = args
                    actual = await self.recorder.store_audio(stream_id, pcm, format)
                    if actual != expected:
                        raise RuntimeError("reserved audio reference differs from persisted audio")
            except Exception as error:
                self._failure = error
            finally:
                self.pending_bytes -= size
                self.queue.task_done()

    async def _finish(self) -> None:
        self._closed = True
        await self.queue.join()
        await self.queue.put(None)
        await self._worker

    async def finish(self) -> None:
        if self._finish_task is None:
            self._finish_task = asyncio.create_task(self._finish())
        await asyncio.shield(self._finish_task)
        if self._failure:
            raise ArtifactBackpressure(
                "artifact writer failed; recording is incomplete"
            ) from self._failure

    def diagnostics(self) -> dict:
        return {
            "queue_high_watermark": self.high_watermark,
            "queue_high_watermark_bytes": self.high_watermark_bytes,
            "pcm_retained_bytes": self.audio_bytes,
            "writer_failed": self._failure is not None,
        }
