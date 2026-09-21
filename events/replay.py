"""Strict offline artifact reading. Partial inspection must be explicitly requested."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from benchmark.contracts import relative_path
from events.schema import AudioRef, NormalizedEvent, RawEvent, RecordingContext


class RecordingError(ValueError):
    pass


def artifact_path(root: Path, relative: str) -> Path:
    relative_path(relative)
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise RecordingError("artifact path escapes the recording directory")
    return path


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_jsonl(path: Path, model: type, *, allow_truncated_tail: bool = False) -> tuple:
    rows = []
    with path.open("rb") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.endswith(b"\n"):
                if allow_truncated_tail:
                    break
                raise RecordingError(f"{path.name}:{line_number}: truncated JSONL line")
            try:
                rows.append(model.model_validate_json(line))
            except (ValidationError, ValueError) as error:
                # Do not echo vendor payloads or credentials in parser errors.
                raise RecordingError(f"{path.name}:{line_number}: invalid event") from error
    return tuple(rows)


@dataclass(frozen=True)
class Recording:
    root: Path
    manifest: dict
    events: tuple[NormalizedEvent, ...]
    raw_events: tuple[RawEvent, ...]

    def audio(self, reference: AudioRef) -> bytes:
        path = artifact_path(self.root, reference.path)
        with path.open("rb") as stream:
            stream.seek(reference.byte_offset)
            data = stream.read(reference.byte_length)
        if len(data) != reference.byte_length:
            raise RecordingError("audio reference extends beyond the saved PCM")
        return data


def _event_references(event: NormalizedEvent) -> list[str]:
    refs = [event.causal_event_id] if event.causal_event_id else []
    payload = event.payload
    for name in (
        "first_chunk_event_id",
        "last_chunk_event_id",
        "trigger_event_id",
        "chunk_event_id",
    ):
        ref = getattr(payload, name, None)
        if ref:
            refs.append(ref)
    for name in ("evidence_event_ids", "evidence"):
        refs.extend(getattr(payload, name, ()))
    return refs


def read_recording(root: str | Path, *, allow_partial: bool = False) -> Recording:
    root = Path(root)
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("schema_version") != "0.1":
            raise RecordingError("unsupported manifest version")
        complete = manifest.get("status") == "complete"
        if not complete and not allow_partial:
            raise RecordingError("recording is incomplete; use allow_partial for inspection only")
        context = RecordingContext.model_validate(manifest["context"])
        if complete:
            for name in ("config.json", "events.jsonl", "raw_events.jsonl"):
                if name not in manifest["files"]:
                    raise RecordingError(f"manifest is missing required file {name}")
        for name, info in manifest.get("files", {}).items():
            path = artifact_path(root, name)
            if path.stat().st_size != info["bytes"] or file_hash(path) != info["sha256"]:
                raise RecordingError(f"artifact integrity mismatch: {name}")
        events = read_jsonl(
            root / "events.jsonl", NormalizedEvent, allow_truncated_tail=not complete
        )
        raw_events = read_jsonl(
            root / "raw_events.jsonl", RawEvent, allow_truncated_tail=not complete
        )
        recording = Recording(root, manifest, events, raw_events)
        ids = {e.event_id for e in events}
        by_id = {e.event_id: e for e in events}
        raw_ids = {e.raw_event_id for e in raw_events}
        if len(ids) != len(events) or len(raw_ids) != len(raw_events):
            raise RecordingError("duplicate event identifiers")
        if complete and (
            len(events) != manifest["event_count"] or len(raw_events) != manifest["raw_event_count"]
        ):
            raise RecordingError("manifest event counts do not match logs")
        for row in (*events, *raw_events):
            if any(getattr(row, k) != v for k, v in context.model_dump().items()):
                raise RecordingError("event context does not match recording")
            if row.clock_id != manifest["clock"]["clock_id"]:
                raise RecordingError("recording mixes clock domains")
        for expected_seq, event in enumerate(events, 1):
            if event.seq != expected_seq:
                raise RecordingError("event sequence has gaps or is out of order")
            if event.raw_event_ref and event.raw_event_ref not in raw_ids:
                raise RecordingError("normalized event references a missing raw event")
            if complete and any(ref not in ids for ref in _event_references(event)):
                raise RecordingError("normalized event references a missing normalized event")
            if complete:
                for name in ("first_chunk_event_id", "last_chunk_event_id", "chunk_event_id"):
                    target_id = getattr(event.payload, name, None)
                    if target_id is None:
                        continue
                    target = by_id[target_id]
                    if (
                        target.event != "assistant_audio_chunk"
                        or target.response_id != event.response_id
                    ):
                        raise RecordingError(
                            "audio anchor references a different response or non-audio event"
                        )
                    if name == "first_chunk_event_id":
                        expected = event.payload.audio_format
                        actual = target.payload.audio_ref
                        if any(
                            getattr(actual, key) != value
                            for key, value in expected.model_dump().items()
                        ):
                            raise RecordingError(
                                "audio start format differs from its referenced chunk"
                            )
            reference = getattr(event.payload, "audio_ref", None)
            if reference is not None:
                if complete and reference.path not in manifest["files"]:
                    raise RecordingError("audio reference is not covered by manifest")
                recording.audio(reference)
        for raw in raw_events:
            if raw.body_ref:
                path = artifact_path(root, raw.body_ref.path)
                if complete and raw.body_ref.path not in manifest["files"]:
                    raise RecordingError("raw blob is not covered by manifest")
                if (
                    path.stat().st_size != raw.body_ref.byte_length
                    or file_hash(path) != raw.body_ref.sha256
                ):
                    raise RecordingError("raw blob reference integrity mismatch")
        if complete:
            starts = [e for e in events if e.event == "session_start"]
            ends = [e for e in events if e.event == "session_end"]
            if len(starts) != 1 or len(ends) != 1 or starts[0].seq >= ends[0].seq:
                raise RecordingError("complete recording needs one ordered session start/end pair")
        return recording
    except RecordingError:
        raise
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise RecordingError("invalid or missing recording artifacts") from error
