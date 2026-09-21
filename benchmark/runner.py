"""Single-turn latency execution. All model wire details remain in adapters."""

import asyncio
import hashlib
from pathlib import Path

from adapters.base import SessionConfig, UnsupportedCapability
from benchmark.config import LatencyProfile
from benchmark.contracts import content_hash
from events.buffered import ArtifactBackpressure, BufferedArtifacts
from events.clock import SystemClock
from events.recorder import EventRecorder
from events.schema import EventDraft, RecordingContext
from scenarios.schema import Scenario, SessionReady
from simulator.audio import read_wav_bytes, render_timeline
from simulator.input import TimingViolation, send_utterance
from simulator.playback import VirtualPlayback


class NoAudioResponse(RuntimeError):
    pass


def require_latency_scenario(scenario: Scenario) -> None:
    if (
        scenario.category not in {"latency", "turn_taking", "pause", "overlap"}
        or len(scenario.actions) != 1
    ):
        raise ValueError(
            "duplex runner accepts one-action latency/turn-taking/pause/overlap scenarios only"
        )
    action = scenario.actions[0]
    if not isinstance(action.trigger, SessionReady) or action.preconditions:
        raise ValueError("Phase 3 latency action must trigger on session_ready")
    if scenario.session.control_profile != "native_server":
        raise ValueError("Phase 3 latency cases do not implement client interruption policies")


async def run_latency_case(
    factory,
    *,
    scenario: Scenario,
    source_wav: bytes,
    output: Path,
    context: RecordingContext,
    config: SessionConfig,
    profile: LatencyProfile,
    secrets: tuple[str, ...] = (),
    warmup: bool = False,
    implementation_hash: str = "unavailable",
    mode: str = "latency_benchmark",
) -> dict:
    require_latency_scenario(scenario)
    action = scenario.actions[0]
    asset = scenario.audio.assets[action.asset]
    if hashlib.sha256(source_wav).hexdigest() != asset.sha256:
        raise ValueError("frozen input hash does not match the scenario")
    pcm = read_wav_bytes(source_wav, config.input_audio)
    clock = SystemClock()
    recorder = EventRecorder(
        output,
        context,
        clock=clock,
        secrets=secrets,
        config={
            "mode": mode,
            "warmup": warmup,
            "scenario_sha256": scenario.sha256,
            "model_config": config.model_dump(mode="json"),
            "model_config_sha256": content_hash(config.model_dump(mode="json")),
            "latency_profile": profile.model_dump(mode="json"),
            "implementation_sha256": implementation_hash,
            "boundary_annotation": asset.boundary_annotation.model_dump(mode="json"),
            "input_kind": asset.provenance.kind,
            "input_asset": {
                "path": asset.path,
                "sha256": asset.sha256,
                "provenance": asset.provenance.model_dump(mode="json"),
                "derivation": asset.derivation,
            },
            "playback_mode": "virtual",
            "input_audio_semantics": "transmitted PCM; packet timings are in events.jsonl",
            "output_audio_semantics": "virtual playback timeline from session_start, including silence gaps",
        },
    )
    await recorder.__aenter__()
    await recorder.write_json("scenario.json", scenario.model_dump(mode="json"))
    await recorder.store_blob(source_wav)
    sink = BufferedArtifacts(
        recorder, capacity=profile.artifact_queue_capacity, max_bytes=profile.artifact_max_bytes
    )
    observed: list[EventDraft] = []
    fatal, response_done, audio_received = asyncio.Event(), asyncio.Event(), asyncio.Event()
    input_end = None
    status, reason = "completed", "response_completed"
    collector = None
    adapter = None
    ack = None

    def publish(event: EventDraft):
        nonlocal input_end
        sink.emit(event)
        observed.append(event)
        if event.event == "user_audio_end":
            input_end = event
        if event.event == "assistant_response_end":
            response_done.set()
        if event.event == "assistant_audio_chunk":
            audio_received.set()
        if event.event == "error" and event.payload.fatal:
            fatal.set()

    playback = VirtualPlayback(context, clock, sink, publish, profile)

    async def collect():
        try:
            while True:
                event = await adapter.receive_event()
                publish(event)
                playback.submit(event)
        except EOFError:
            return
        except Exception:
            fatal.set()
            raise

    async def wait_for(signal: asyncio.Event, timeout: float | None, *, require_audio=False):
        ready, failed = asyncio.create_task(signal.wait()), asyncio.create_task(fatal.wait())
        ended = asyncio.create_task(response_done.wait()) if require_audio else None
        try:
            await asyncio.wait(
                {ready, failed} | ({ended} if ended else set()),
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if fatal.is_set():
                raise RuntimeError("adapter_or_recording_failed")
            if require_audio and response_done.is_set() and not audio_received.is_set():
                raise NoAudioResponse("response_ended_without_audio")
            if not signal.is_set():
                raise TimeoutError("response_timeout")
        finally:
            tasks = [ready, failed] + ([ended] if ended else [])
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    try:
        async with asyncio.timeout(scenario.termination.max_case_duration_ms / 1000):
            adapter = factory(context, sink, clock=clock)
            adapter.capabilities().require(*scenario.capabilities_required)
            await adapter.connect()
            collector = asyncio.create_task(collect())
            ack = await adapter.configure(config)
            await send_utterance(
                adapter,
                sink,
                context,
                clock,
                publish,
                action=action,
                asset=asset,
                pcm=pcm,
                chunk_ms=scenario.audio.chunk_ms,
                profile=profile,
            )
            deadline = (
                input_end.timestamp_monotonic_ns
                + scenario.termination.response_timeout_ms * 1_000_000
            )
            await wait_for(
                audio_received,
                max(0, (deadline - clock.now().timestamp_monotonic_ns) / 1e9),
                require_audio=True,
            )
            await wait_for(
                response_done, None
            )  # Completion is bounded by max_case_duration, not TTFA timeout.
    except UnsupportedCapability:
        status, reason = "unsupported", "required_capability_unavailable"
    except TimingViolation as error:
        status, reason = "invalid", str(error)
    except TimeoutError:
        status, reason = (
            "model_failed",
            "response_completion_timeout" if audio_received.is_set() else "response_timeout",
        )
    except NoAudioResponse:
        status, reason = "model_failed", "no_audio"
    except (Exception, asyncio.CancelledError) as error:
        status, reason = "infra_failed", type(error).__name__
    finally:
        if adapter:
            try:
                await adapter.close()
            except Exception:
                status, reason = "infra_failed", "adapter_close_failed"
        if collector:
            try:
                await asyncio.wait_for(collector, 5)
            except Exception:
                if status == "completed":
                    status, reason = "infra_failed", "event_collection_failed"
        try:
            await asyncio.wait_for(playback.finish(), scenario.termination.drain_timeout_ms / 1000)
        except TimeoutError:
            status, reason = "invalid", "playback_drain_timeout"
            await playback.abort()
        except TimingViolation as error:
            status, reason = "invalid", str(error)
        except (Exception, asyncio.CancelledError):
            status, reason = "invalid", "playback_drain_failed"

    if status == "completed":
        ends = [event for event in observed if event.event == "assistant_response_end"]
        if not ends or any(event.payload.status != "completed" for event in ends):
            status, reason = "model_failed", "response_not_completed"
        elif not any(event.event == "assistant_audio_chunk" for event in observed):
            status, reason = "model_failed", "no_audio"
    case_end = EventDraft(
        **context.model_dump(),
        **clock.now().model_dump(),
        event="case_end",
        source="system",
        producer="benchmark.runner",
        timing={"basis": "inferred"},
        payload={"status": status, "reason": reason},
    )
    try:
        publish(case_end)
        await sink.finish()
    except ArtifactBackpressure:
        status, reason = "infra_failed", "artifact_write_failed"
        try:
            await sink.finish()
        except ArtifactBackpressure:
            pass
        trial = {
            "status": status,
            "reason": reason,
            "warmup": warmup,
            "artifact_queue": sink.diagnostics(),
        }
        try:
            await recorder.write_json("trial.json", trial)
        finally:
            await recorder.close(complete=False, reason=reason)
        return trial
    session_start = next((event for event in observed if event.event == "session_start"), None)
    session_end = next((event for event in observed if event.event == "session_end"), None)
    origin_ns = (
        session_start.timestamp_monotonic_ns
        if session_start
        else recorder.origin.timestamp_monotonic_ns
    )
    received = [event for event in observed if event.event == "assistant_audio_chunk"]
    sent = [event for event in observed if event.event == "user_audio_chunk"]
    input_pcm = b"".join(sink.audio_part(event.payload.audio_ref) for event in sent)
    output_pcm = b"".join(sink.audio(event.payload.audio_ref) for event in received)
    timeline = render_timeline(playback.segments, origin_ns=origin_ns, format=config.output_audio)
    await recorder.store_wav("input.wav", input_pcm, config.input_audio)
    await recorder.store_wav("output_received.wav", output_pcm, config.output_audio)
    await recorder.store_wav("output.wav", timeline, config.output_audio)
    await recorder.write_json(
        "transcript.json",
        [
            {
                "event": e.event,
                "event_id": e.event_id,
                "turn_id": e.turn_id,
                "response_id": e.response_id,
                **e.payload.model_dump(mode="json"),
            }
            for e in observed
            if e.event in {"user_text_done", "assistant_text_done"}
        ],
    )
    await recorder.write_json(
        "session_config.json", ack.model_dump(mode="json") if ack else {"status": "unavailable"}
    )
    trial = {
        "status": status,
        "reason": reason,
        "warmup": warmup,
        "playback_mode": "virtual",
        "output_origin_monotonic_ns": origin_ns,
        "artifact_queue": sink.diagnostics(),
        "max_playback_lateness_ms": playback.max_lateness_ns / 1e6,
        "backend": adapter.diagnostics() if adapter else {},
        "capabilities": adapter.capabilities().model_dump(mode="json") if adapter else {},
    }
    await recorder.write_json("trial.json", trial)
    await recorder.close(
        complete=bool(session_start and session_end),
        reason=None if session_start and session_end else reason,
    )
    return trial


async def run_case(
    factory,
    *,
    scenario,
    source_wavs,
    output,
    context,
    config,
    profile,
    secrets=(),
    warmup=False,
    implementation_hash="unavailable",
):
    if scenario.category == "latency":
        action = scenario.actions[0]
        return await run_latency_case(
            factory,
            scenario=scenario,
            source_wav=source_wavs[scenario.audio.assets[action.asset].path],
            output=output,
            context=context,
            config=config,
            profile=profile,
            secrets=secrets,
            warmup=warmup,
            implementation_hash=implementation_hash,
        )
    if scenario.category in {"turn_taking", "pause", "overlap"}:
        return await run_latency_case(
            factory,
            scenario=scenario,
            source_wav=source_wavs[scenario.audio.assets[scenario.actions[0].asset].path],
            output=output,
            context=context,
            config=config,
            profile=profile,
            secrets=secrets,
            warmup=warmup,
            implementation_hash=implementation_hash,
            mode="duplex_benchmark",
        )
    if scenario.category == "interruption":
        from benchmark.interruption import run_interruption_case

        return await run_interruption_case(
            factory,
            scenario=scenario,
            source_wavs=source_wavs,
            output=output,
            context=context,
            config=config,
            profile=profile,
            secrets=secrets,
            warmup=warmup,
            implementation_hash=implementation_hash,
        )
    if scenario.category == "backchannel":
        from benchmark.backchannel import run_backchannel_case

        return await run_backchannel_case(
            factory,
            scenario=scenario,
            source_wavs=source_wavs,
            output=output,
            context=context,
            config=config,
            profile=profile,
            secrets=secrets,
            warmup=warmup,
            implementation_hash=implementation_hash,
        )
    raise ValueError(f"unsupported realtime category: {scenario.category}")
