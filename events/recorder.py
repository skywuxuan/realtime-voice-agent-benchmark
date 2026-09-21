"""One recorder per attempt; serialized disk writes outside the event loop."""

import asyncio
import hashlib
import io
import os
import wave
from pathlib import Path
from typing import Literal

from benchmark.audio import AudioFormat, AudioRef
from benchmark.contracts import canonical_json
from events.clock import Clock, ClockReading, SystemClock
from events.redaction import Redactor
from events.replay import RecordingError, artifact_path, file_hash, read_recording
from events.schema import BlobRef, EventDraft, NormalizedEvent, RawEvent, RecordingContext


def audio_stream_path(stream_id: str) -> str:
    return f"audio/{hashlib.sha256(stream_id.encode()).hexdigest()}.pcm"


class EventRecorder:
    def __init__(
        self,
        root: str | Path,
        context: RecordingContext,
        *,
        clock: Clock | None = None,
        config: dict | None = None,
        secrets: tuple[str, ...] = (),
    ) -> None:
        self.root = Path(root)
        self.context = context
        self.clock = clock or SystemClock()
        self.origin = self.clock.now()
        self.redactor = Redactor(secrets)
        self.config = config or {}
        self._lock = asyncio.Lock()
        self._opened = False
        self._closed = False
        self._poisoned = False
        self._ids: set[str] = set()
        self._raw_ids: set[str] = set()
        self._streams: dict[str, tuple[AudioFormat, int, str]] = {}
        self._files = {"config.json", "events.jsonl", "raw_events.jsonl"}

    async def _io(self, function, *args):
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Finish an in-flight write before allowing close or another writer.
            self._poisoned = True
            try:
                await task
            finally:
                raise
        except OSError:
            self._poisoned = True
            raise

    async def __aenter__(self) -> "EventRecorder":
        async with self._lock:
            if self._opened or self._closed:
                raise RecordingError("recorder cannot be reopened")
            await self._io(self._open)
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.close(complete=exc_type is None, reason=exc_type.__name__ if exc_type else None)

    def _open(self) -> None:
        self.root.mkdir(parents=True, exist_ok=False)
        (self.root / "events.jsonl").touch(exist_ok=False)
        (self.root / "raw_events.jsonl").touch(exist_ok=False)
        clean_config, _ = self.redactor.clean(self.config)
        (self.root / "config.json").write_text(
            canonical_json(clean_config) + "\n", encoding="utf-8"
        )
        self._opened = True
        self._write_manifest("recording", None, hash_files=False)

    def _require_open(self) -> None:
        if not self._opened or self._closed:
            raise RecordingError("recorder is not open")
        if self._poisoned:
            raise RecordingError("recorder had an interrupted write; finalize as incomplete")

    def _check_context(self, row) -> None:
        if any(getattr(row, k) != v for k, v in self.context.model_dump().items()):
            raise RecordingError("event belongs to another recording context")
        if row.clock_id != self.origin.clock_id:
            raise RecordingError("event belongs to another clock domain")

    def _append(self, name: str, row: object) -> None:
        with (self.root / name).open("a", encoding="utf-8") as stream:
            stream.write(canonical_json(row) + "\n")
            stream.flush()

    async def record(
        self, draft: EventDraft, *, recorded_at: ClockReading | None = None
    ) -> NormalizedEvent:
        reading = recorded_at or self.clock.now()  # Queue ingress, not disk completion.
        # Snapshot mutable nested payloads before crossing the asynchronous boundary.
        data = draft.model_dump(mode="json")
        async with self._lock:
            self._require_open()
            if reading.clock_id != self.origin.clock_id:
                raise RecordingError("recorder clock domain changed")
            return await self._io(self._record, data, reading.timestamp_monotonic_ns)

    def _record(self, data: dict, recorded_ns: int) -> NormalizedEvent:
        data, _ = self.redactor.clean(data)
        event = NormalizedEvent.model_validate(
            {
                **data,
                "seq": len(self._ids) + 1,
                "recorded_monotonic_ns": recorded_ns,
            }
        )
        self._check_context(event)
        if event.event_id in self._ids:
            raise RecordingError("duplicate normalized event_id")
        if event.raw_event_ref and event.raw_event_ref not in self._raw_ids:
            raise RecordingError("record raw event before its normalized counterpart")
        self._append("events.jsonl", event.model_dump(mode="json"))
        self._ids.add(event.event_id)
        return event

    async def record_raw(self, raw: RawEvent) -> RawEvent:
        data = raw.model_dump(mode="json")
        async with self._lock:
            self._require_open()
            return await self._io(self._record_raw, data)

    def _record_raw(self, data: dict) -> RawEvent:
        data, removed = self.redactor.clean(data)
        data["redacted_fields"] = sorted(set(data["redacted_fields"]) | set(removed))
        raw = RawEvent.model_validate(data)
        self._check_context(raw)
        if raw.raw_event_id in self._raw_ids:
            raise RecordingError("duplicate raw_event_id")
        self._append("raw_events.jsonl", raw.model_dump(mode="json"))
        self._raw_ids.add(raw.raw_event_id)
        return raw

    async def store_audio(self, stream_id: str, pcm: bytes, format: AudioFormat) -> AudioRef:
        pcm = bytes(pcm)
        if not stream_id or not pcm or len(pcm) % format.bytes_per_sample_frame:
            raise ValueError("stream and nonempty whole PCM sample frames are required")
        async with self._lock:
            self._require_open()
            return await self._io(self._store_audio, stream_id, pcm, format)

    def _store_audio(self, stream_id: str, pcm: bytes, format: AudioFormat) -> AudioRef:
        name = audio_stream_path(stream_id)
        prior_format, offset, _ = self._streams.get(stream_id, (format, 0, name))
        if format != prior_format:
            raise RecordingError("audio format cannot change within a stream")
        path = artifact_path(self.root, name)
        path.parent.mkdir(exist_ok=True)
        with path.open("ab") as stream:
            if stream.tell() != offset:
                raise RecordingError("audio file changed outside the recorder")
            stream.write(pcm)
        self._streams[stream_id] = (format, offset + len(pcm), name)
        self._files.add(name)
        return AudioRef(
            **format.model_dump(),
            path=name,
            byte_offset=offset,
            byte_length=len(pcm),
            sample_offset=offset // format.bytes_per_sample_frame,
            sample_count=len(pcm) // format.bytes_per_sample_frame,
        )

    async def store_blob(self, data: bytes) -> BlobRef:
        """Store already-sanitized binary/audio data; never pass credential-bearing JSON."""
        data = bytes(data)
        async with self._lock:
            self._require_open()
            return await self._io(self._store_blob, data)

    def _store_blob(self, data: bytes) -> BlobRef:
        digest = hashlib.sha256(data).hexdigest()
        name = f"blobs/{digest}.bin"
        path = artifact_path(self.root, name)
        path.parent.mkdir(exist_ok=True)
        if not path.exists():
            path.write_bytes(data)
        elif file_hash(path) != digest:
            raise RecordingError("existing content-addressed blob was modified")
        self._files.add(name)
        return BlobRef(path=name, sha256=digest, byte_length=len(data))

    async def write_json(self, name: str, value: object) -> None:
        """Write a redacted, manifest-tracked sidecar exactly once."""
        if not name.endswith(".json"):
            raise ValueError("JSON sidecar must have a .json suffix")
        clean, _ = self.redactor.clean(value)
        data = (canonical_json(clean) + "\n").encode("utf-8")
        async with self._lock:
            self._require_open()
            await self._io(self._write_sidecar, name, data)

    async def store_wav(self, name: str, pcm: bytes, format: AudioFormat) -> None:
        if not name.endswith(".wav") or len(pcm) % format.bytes_per_sample_frame:
            raise ValueError("WAV needs a .wav suffix and whole PCM sample frames")
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(format.channels)
            wav.setsampwidth(2)
            wav.setframerate(format.sample_rate_hz)
            wav.writeframes(pcm)
        async with self._lock:
            self._require_open()
            await self._io(self._write_sidecar, name, buffer.getvalue())

    def _write_sidecar(self, name: str, data: bytes) -> None:
        if name in self._files or name == "manifest.json" or name.startswith(("audio/", "blobs/")):
            raise RecordingError("cannot replace a managed artifact")
        path = artifact_path(self.root, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(data)
        self._files.add(name)

    def _write_manifest(self, status: str, reason: str | None, *, hash_files: bool = True) -> None:
        files = {}
        if hash_files:
            for name in sorted(self._files):
                path = artifact_path(self.root, name)
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
                files[name] = {"sha256": file_hash(path), "bytes": path.stat().st_size}
        manifest = {
            "schema_version": "0.1",
            "status": status,
            "reason": reason,
            "context": self.context.model_dump(),
            "clock": self.origin.model_dump(mode="json"),
            "event_count": len(self._ids),
            "raw_event_count": len(self._raw_ids),
            "files": files,
            "audio_streams": {
                key: {"format": fmt.model_dump(), "byte_length": length, "path": name}
                for key, (fmt, length, name) in self._streams.items()
            },
        }
        temp = self.root / "manifest.json.tmp"
        with temp.open("w", encoding="utf-8") as stream:
            stream.write(canonical_json(manifest) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(self.root / "manifest.json")

    async def close(self, *, complete: bool = True, reason: str | None = None) -> None:
        async with self._lock:
            if self._closed:
                return
            if not self._opened:
                raise RecordingError("recorder was never opened")
            clean_reason, _ = self.redactor.clean(reason)
            await self._io(self._close, complete and not self._poisoned, clean_reason)

    def _close(self, complete: bool, reason: str | None) -> None:
        status: Literal["complete", "incomplete"] = "complete" if complete else "incomplete"
        self._write_manifest(status, reason)
        self._closed = True
        if complete:
            try:
                read_recording(self.root)
            except RecordingError:
                self._write_manifest("incomplete", "integrity_validation_failed")
                raise
