"""A single real-connection probe, independent of vendor event names and SDKs.

This checks transport and artifact integrity. It is not a latency/interruption suite.
"""

import asyncio
import hashlib
import platform
import uuid
import wave
from collections.abc import Callable
from pathlib import Path

from adapters.base import InterruptRequest, RealtimeModelAdapter, SessionConfig
from benchmark.audio import AudioFrame, AudioRef
from events.clock import SystemClock
from events.recorder import EventRecorder
from events.replay import artifact_path
from events.schema import EventDraft, RecordingContext


def load_pcm_wav(path: Path, config: SessionConfig) -> bytes:
    with wave.open(str(path), "rb") as wav:
        if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth(), wav.getcomptype()) != (
            config.input_audio.sample_rate_hz,
            config.input_audio.channels,
            2,
            "NONE",
        ):
            raise ValueError("input WAV must match the configured PCM16 sample rate and channels")
        pcm = wav.readframes(wav.getnframes())
        if len(pcm) != wav.getnframes() * config.input_audio.bytes_per_sample_frame:
            raise ValueError("input WAV is truncated")
        if not pcm or wav.getnframes() > config.input_audio.sample_rate_hz * 30:
            raise ValueError("probe input must be between zero and 30 seconds")
        return pcm


async def run_connection_probe(
    factory: Callable[..., RealtimeModelAdapter],
    *,
    audio_path: Path,
    output: Path,
    config: SessionConfig,
    secrets: tuple[str, ...] = (),
    chunk_ms: int = 20,
    tail_silence_ms: int = 1500,
    response_timeout_s: float = 30,
    cancel_after_chunks: int | None = None,
    provenance: dict | None = None,
) -> dict:
    pcm = load_pcm_wav(audio_path, config)
    rate, width = config.input_audio.sample_rate_hz, config.input_audio.bytes_per_sample_frame
    if chunk_ms <= 0 or chunk_ms * rate % 1000 or tail_silence_ms < 0:
        raise ValueError("invalid chunk or trailing silence duration")
    if cancel_after_chunks is not None and (
        cancel_after_chunks < 1 or config.control_profile != "client_forced"
    ):
        raise ValueError("cancel probe requires positive chunk count and client_forced profile")
    chunk_bytes = rate * chunk_ms // 1000 * width
    tail = (
        b"\0" * (rate * tail_silence_ms // 1000 * width)
        if config.turn_mode == "server_vad"
        else b""
    )
    full_pcm = pcm + tail
    context = RecordingContext(
        run_id=f"probe_{uuid.uuid4().hex}",
        scenario_id="connection_probe_001",
        attempt_id="attempt_001",
        session_id=f"session_{uuid.uuid4().hex}",
    )
    clock = SystemClock()
    recorder = EventRecorder(
        output,
        context,
        clock=clock,
        secrets=secrets,
        config={
            "mode": "connection_probe",
            "run_id": context.run_id,
            "model_config": config.model_dump(mode="json"),
            "chunk_ms": chunk_ms,
            "tail_silence_ms": tail_silence_ms if tail else 0,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "input_source_sha256": hashlib.sha256(audio_path.read_bytes()).hexdigest(),
            "input_provenance": provenance or {},
            "code_revision": None,
            "code_revision_reason": "not supplied; no Git revision assumed",
            "playback_mode": "not_run",
        },
    )
    await recorder.__aenter__()
    adapter = factory(context, recorder, clock=clock)
    observed: list[EventDraft] = []
    sent_bytes = bytearray()
    response_done, fatal = asyncio.Event(), asyncio.Event()
    collector = None
    cancel_task = None
    failure: str | None = None
    input_end = None
    ack = None
    pacing_errors = []

    async def emit(kind, payload, *, reading=None, **fields):
        event = EventDraft(
            **context.model_dump(),
            **(reading or clock.now()).model_dump(),
            event=kind,
            producer="connection_probe",
            source="user",
            timing={"basis": "client_send"},
            payload=payload,
            **fields,
        )
        await recorder.record(event)
        observed.append(event)
        return event

    async def cancel(response_id):
        try:
            return await adapter.interrupt(
                InterruptRequest(
                    target_response_id=response_id, reason="explicit control-path probe"
                )
            )
        except Exception:
            fatal.set()
            raise

    async def collect():
        nonlocal cancel_task
        received_chunks = 0
        try:
            while True:
                event = await adapter.receive_event()
                await recorder.record(event)
                observed.append(event)
                if event.event == "assistant_audio_chunk":
                    received_chunks += 1
                    if cancel_after_chunks == received_chunks and cancel_task is None:
                        cancel_task = asyncio.create_task(cancel(event.response_id))
                elif event.event == "assistant_response_end":
                    response_done.set()
                    if event.payload.status not in {"completed", "cancelled"}:
                        fatal.set()
                elif event.event == "error" and event.payload.fatal:
                    fatal.set()
        except EOFError:
            return
        except Exception:
            fatal.set()
            raise

    async def wait_for_response():
        ready, failed = asyncio.create_task(response_done.wait()), asyncio.create_task(fatal.wait())
        try:
            done, _ = await asyncio.wait(
                {ready, failed}, timeout=response_timeout_s, return_when=asyncio.FIRST_COMPLETED
            )
            if fatal.is_set():
                raise RuntimeError("adapter_error; inspect recorded error events")
            if ready not in done:
                raise TimeoutError("response_timeout")
        finally:
            for task in (ready, failed):
                task.cancel()
            await asyncio.gather(ready, failed, return_exceptions=True)

    try:
        async with asyncio.timeout(90):
            await adapter.connect()
            collector = asyncio.create_task(collect())
            ack = await adapter.configure(config)
            reference = await recorder.store_audio("input_source", full_pcm, config.input_audio)
            start = clock.now()
            await emit(
                "user_audio_start",
                {
                    "action_id": "send_input",
                    "asset_id": "probe_input",
                    "sample_index": 0,
                    "annotation_source": "file_segment_boundary_not_speech_annotation",
                },
                reading=start,
                turn_id="t1",
                stream_id="input",
            )
            # Split at the original file boundary even when it falls inside a frame.
            offset = 0
            index = 0
            while offset < len(full_pcm):
                end = min(offset + chunk_bytes, len(pcm) if offset < len(pcm) else len(full_pcm))
                chunk = full_pcm[offset:end]
                planned = start.timestamp_monotonic_ns + int(end / width / rate * 1e9)
                await asyncio.sleep(max(0, (planned - clock.now().timestamp_monotonic_ns) / 1e9))
                receipt = await adapter.send_audio(
                    AudioFrame(
                        pcm=chunk,
                        format=config.input_audio,
                        stream_id="input",
                        turn_id="t1",
                        chunk_index=index,
                        sample_offset=offset // width,
                    )
                )
                sent_bytes.extend(chunk)
                pacing_errors.append((receipt.started.timestamp_monotonic_ns - planned) / 1e6)
                chunk_ref = AudioRef(
                    **config.input_audio.model_dump(),
                    path=reference.path,
                    byte_offset=offset,
                    byte_length=len(chunk),
                    sample_offset=offset // width,
                    sample_count=len(chunk) // width,
                )
                await emit(
                    "user_audio_chunk",
                    {
                        "audio_ref": chunk_ref.model_dump(),
                        "chunk_index": index,
                        "planned_send_ns": planned,
                        "send_started_ns": receipt.started.timestamp_monotonic_ns,
                        "send_completed_ns": receipt.completed.timestamp_monotonic_ns,
                        "silence": offset >= len(pcm),
                    },
                    reading=receipt.completed,
                    turn_id="t1",
                    stream_id="input",
                )
                if end == len(pcm):
                    input_end = await emit(
                        "user_audio_end",
                        {
                            "action_id": "send_input",
                            "end_sample": len(pcm) // width,
                            "annotation_source": "file_segment_end_proxy",
                        },
                        reading=receipt.completed,
                        turn_id="t1",
                        stream_id="input",
                    )
                offset, index = end, index + 1
            if config.turn_mode == "manual":
                await adapter.commit_turn("t1")
            await wait_for_response()
            if cancel_task:
                await cancel_task
    except (Exception, asyncio.CancelledError) as error:
        failure = type(error).__name__ + ": " + str(error)
    finally:
        try:
            await adapter.close()
        except Exception as error:
            failure = failure or f"close: {type(error).__name__}"
        if collector:
            try:
                await asyncio.wait_for(collector, 5)
            except Exception as error:
                failure = failure or f"collector: {type(error).__name__}"
        if cancel_task:
            if not cancel_task.done():
                cancel_task.cancel()
            result = await asyncio.gather(cancel_task, return_exceptions=True)
            if isinstance(result[0], BaseException):
                failure = failure or f"cancel_probe: {type(result[0]).__name__}"

    chunks = [event for event in observed if event.event == "assistant_audio_chunk"]
    received_pcm = bytearray()
    for event in chunks:
        ref = event.payload.audio_ref
        with artifact_path(output, ref.path).open("rb") as stream:
            stream.seek(ref.byte_offset)
            received_pcm.extend(stream.read(ref.byte_length))
    ends = [event for event in observed if event.event == "assistant_response_end"]
    cancellations = [event for event in observed if event.event == "assistant_cancelled"]
    session_ends = [event for event in observed if event.event == "session_end"]
    success = bool(
        chunks
        and ends
        and session_ends
        and session_ends[-1].payload.complete
        and not failure
        and not fatal.is_set()
    )
    if cancel_after_chunks is not None:
        success = success and bool(cancellations)
    clean_failure, _ = recorder.redactor.clean(failure)
    summary = {
        "mode": "connection_probe_not_benchmark",
        "success": success,
        "run_id": context.run_id,
        "model": config.model,
        "voice": config.voice,
        "turn_mode": config.turn_mode,
        "control_profile": config.control_profile,
        "failure": clean_failure,
        "received_audio_chunks": len(chunks),
        "received_audio_duration_ms": len(received_pcm)
        / config.output_audio.bytes_per_sample_frame
        / config.output_audio.sample_rate_hz
        * 1000,
        "first_audio_after_file_end_ms": (
            (chunks[0].timestamp_monotonic_ns - input_end.timestamp_monotonic_ns) / 1e6
        )
        if chunks and input_end
        else None,
        "latency_boundary": "input file end proxy, not annotated speech end; no TTFA percentiles",
        "response_statuses": [event.payload.status for event in ends],
        "cancel_confirmations": len(cancellations),
        "max_send_lateness_ms": max(pacing_errors, default=None),
        "capabilities": adapter.capabilities().model_dump(mode="json"),
        "backend": adapter.diagnostics(),
        "playback_mode": "not_run",
        "output_audio_semantics": "received PCM concatenation, not playback timeline",
    }
    transcripts = [
        {
            "source": event.producer,
            "event": event.event,
            "turn_id": event.turn_id,
            "response_id": event.response_id,
            "event_id": event.event_id,
            **event.payload.model_dump(mode="json"),
        }
        for event in observed
        if event.event in {"user_text_done", "assistant_text_done"}
    ]
    await recorder.store_wav("input.wav", bytes(sent_bytes), config.input_audio)
    await recorder.store_wav("output_received.wav", bytes(received_pcm), config.output_audio)
    await recorder.write_json(
        "session_config.json", ack.model_dump(mode="json") if ack else {"status": "not_configured"}
    )
    await recorder.write_json("transcript.json", transcripts)
    await recorder.write_json("probe.json", summary)
    await recorder.close(complete=success, reason=None if success else "probe_failed_or_incomplete")
    return summary
