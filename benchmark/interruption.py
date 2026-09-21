"""Two-turn native barge-in execution and offline evidence capture."""

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
from scenarios.schema import AfterEvent, Scenario, SessionReady
from simulator.audio import read_wav_bytes, render_timeline
from simulator.input import TimingViolation, send_utterance
from simulator.playback import VirtualPlayback


class InterruptionExecutionError(RuntimeError):
    pass


def require_interruption_scenario(scenario: Scenario) -> None:
    if scenario.category not in {"interruption", "backchannel"} or len(scenario.actions) != 2:
        raise ValueError("interruption runner requires exactly two actions")
    first, second = scenario.actions
    if not isinstance(first.trigger, SessionReady) or first.stimulus != "utterance":
        raise ValueError("first interruption action must start at session_ready")
    if not isinstance(second.trigger, AfterEvent) or second.stimulus != scenario.category:
        raise ValueError("second interruption action must use an after_event trigger")
    if "target_response_id" not in second.trigger.bind:
        raise ValueError("interruption trigger must bind target_response_id")
    if scenario.session.control_profile != "native_server":
        raise ValueError("native interruption runner does not execute client-forced profiles")


def _trigger_matches(event: EventDraft, trigger: AfterEvent) -> bool:
    if event.event != trigger.event:
        return False
    for field, value in trigger.where.items():
        if field == "event_id" and event.event_id != value:
            return False
        if field == "action_id" and getattr(event.payload, "action_id", None) != value:
            return False
        if field == "turn_id" and event.turn_id != value:
            return False
        if field == "response_id" and event.response_id != value:
            return False
    return True


async def run_interruption_case(
    factory,
    *,
    scenario: Scenario,
    source_wavs: dict[str, bytes],
    output: Path,
    context: RecordingContext,
    config: SessionConfig,
    profile: LatencyProfile,
    secrets: tuple[str, ...] = (),
    warmup: bool = False,
    implementation_hash: str = "unavailable",
) -> dict:
    require_interruption_scenario(scenario)
    is_backchannel = scenario.category == "backchannel"
    marker_kind = "backchannel_start" if is_backchannel else "interrupt_start"
    first_action, interrupt_action = scenario.actions
    first_asset = scenario.audio.assets[first_action.asset]
    interrupt_asset = scenario.audio.assets[interrupt_action.asset]
    for asset in (first_asset, interrupt_asset):
        data = source_wavs[asset.path]
        if hashlib.sha256(data).hexdigest() != asset.sha256:
            raise ValueError(f"frozen input hash mismatch: {asset.path}")
    recorder = EventRecorder(
        output,
        context,
        clock=SystemClock(),
        secrets=secrets,
        config={
            "mode": f"{scenario.category}_benchmark",
            "warmup": warmup,
            "scenario_sha256": scenario.sha256,
            "model_config": config.model_dump(mode="json"),
            "model_config_sha256": content_hash(config.model_dump(mode="json")),
            "latency_profile": profile.model_dump(mode="json"),
            "implementation_sha256": implementation_hash,
            "playback_mode": "virtual",
            "control_profile": config.control_profile,
            "stimulus_policy": "audio_only_native_server",
            "boundary_annotations": {
                first_action.asset: first_asset.boundary_annotation.model_dump(mode="json"),
                interrupt_action.asset: interrupt_asset.boundary_annotation.model_dump(mode="json"),
            },
            "input_assets": {
                first_action.asset: {
                    "path": first_asset.path,
                    "sha256": first_asset.sha256,
                    "provenance": first_asset.provenance.model_dump(mode="json"),
                    "derivation": first_asset.derivation,
                },
                interrupt_action.asset: {
                    "path": interrupt_asset.path,
                    "sha256": interrupt_asset.sha256,
                    "provenance": interrupt_asset.provenance.model_dump(mode="json"),
                    "derivation": interrupt_asset.derivation,
                },
            },
        },
    )
    await recorder.__aenter__()
    await recorder.write_json("scenario.json", scenario.model_dump(mode="json"))
    for asset in (first_asset, interrupt_asset):
        await recorder.store_blob(source_wavs[asset.path])
    sink = BufferedArtifacts(
        recorder, capacity=profile.artifact_queue_capacity, max_bytes=profile.artifact_max_bytes
    )
    observed: list[EventDraft] = []
    changed = asyncio.Event()
    fatal = asyncio.Event()
    disconnected = asyncio.Event()
    closing = False
    first_sender = None
    stop_tail = asyncio.Event()
    adapter = None
    collector = None
    playback = VirtualPlayback(context, recorder.clock, sink, None, profile)
    target_response_id: str | None = None
    interrupt_marker_written = False
    stimulus_evidence: dict = {}

    # Replace the callback after creating playback so all events share one append path.
    def publish(event: EventDraft):
        nonlocal interrupt_marker_written, stimulus_evidence
        if (
            event.event == "user_audio_start"
            and event.payload.action_id == interrupt_action.action_id
            and target_response_id
            and not interrupt_marker_written
        ):
            stimulus_evidence = preconditions()
            marker = EventDraft(
                **context.model_dump(),
                clock_id=event.clock_id,
                timestamp_monotonic_ns=event.timestamp_monotonic_ns,
                wall_clock_timestamp=event.wall_clock_timestamp,
                event=marker_kind,
                source="user",
                producer=f"simulator.{scenario.category}",
                timing={"basis": "simulator_boundary"},
                turn_id=interrupt_action.turn_id,
                response_id=target_response_id,
                stream_id="input",
                causal_event_id=event.event_id,
                payload={
                    "action_id": interrupt_action.action_id,
                    "target_response_id": target_response_id,
                    "intent_revision": interrupt_action.intent_revision,
                },
            )
            sink.emit(marker)
            observed.append(marker)
            interrupt_marker_written = True
        sink.emit(event)
        observed.append(event)
        if (
            is_backchannel
            and event.event == "user_audio_end"
            and event.payload.action_id == interrupt_action.action_id
        ):
            end_marker = EventDraft(
                **context.model_dump(),
                clock_id=event.clock_id,
                timestamp_monotonic_ns=event.timestamp_monotonic_ns,
                wall_clock_timestamp=event.wall_clock_timestamp,
                event="backchannel_end",
                source="user",
                producer="simulator.backchannel",
                timing={"basis": "simulator_boundary"},
                turn_id=interrupt_action.turn_id,
                response_id=target_response_id,
                stream_id="input",
                causal_event_id=event.event_id,
                payload={
                    "action_id": interrupt_action.action_id,
                    "target_response_id": target_response_id,
                },
            )
            sink.emit(end_marker)
            observed.append(end_marker)
        if event.event == "error" and event.payload.fatal:
            fatal.set()
        if event.event in {
            "assistant_audio_chunk",
            "assistant_audio_end",
            "assistant_cancelled",
        }:
            playback.submit(event)
        changed.set()

    def preconditions():
        remaining_ms = playback.remaining_ms(target_response_id)
        generating = any(
            e.event == "assistant_response_start" and e.response_id == target_response_id
            for e in observed
        ) and not any(
            e.event == "assistant_response_end" and e.response_id == target_response_id
            for e in observed
        )
        checks = {}
        for precondition in interrupt_action.preconditions:
            if precondition.type == "minimum_continuation_evidence":
                checks[precondition.type] = remaining_ms >= precondition.remaining_ms
            elif precondition.type == "response_still_playing":
                checks[precondition.type] = (
                    target_response_id in playback.started and remaining_ms > 0
                )
            else:
                checks[precondition.type] = generating
        return {
            "checks": checks,
            "buffer_remaining_ms": remaining_ms,
            "basis": "received_unplayed_pcm",
            "valid": all(checks.values()),
        }

    def check_transport():
        if fatal.is_set() or disconnected.is_set():
            raise ConnectionError("transport_failed_during_observation")
        if first_sender and first_sender.done():
            first_sender.result()

    async def wait_event(predicate, deadline_ns, reason, occurrence=1):
        while True:
            check_transport()
            matches = [event for event in observed if predicate(event)]
            if len(matches) >= occurrence:
                return matches[occurrence - 1]
            remaining = (deadline_ns - recorder.clock.now().timestamp_monotonic_ns) / 1e9
            if remaining <= 0:
                raise TimeoutError(reason)
            changed.clear()
            try:
                await asyncio.wait_for(changed.wait(), remaining)
            except TimeoutError:
                raise TimeoutError(reason) from None

    playback.publish = publish
    status, reason = "completed", f"{scenario.category}_observed"
    ack = None
    cleanup_warnings: list[str] = []

    async def collect():
        try:
            while True:
                event = await adapter.receive_event()
                publish(event)
        except EOFError:
            if not closing:
                disconnected.set()
        except Exception:
            fatal.set()
            raise
        finally:
            changed.set()

    try:
        async with asyncio.timeout(scenario.termination.max_case_duration_ms / 1000):
            adapter = factory(context, sink, clock=recorder.clock)
            adapter.capabilities().require(*scenario.capabilities_required)
            await adapter.connect()
            collector = asyncio.create_task(collect())
            ack = await adapter.configure(config)
            first_sender = asyncio.create_task(
                send_utterance(
                    adapter,
                    sink,
                    context,
                    recorder.clock,
                    publish,
                    action=first_action,
                    asset=first_asset,
                    pcm=read_wav_bytes(source_wavs[first_asset.path], config.input_audio),
                    chunk_ms=scenario.audio.chunk_ms,
                    profile=profile,
                    stop_tail=stop_tail,
                )
            )
            first_sender.add_done_callback(lambda _: changed.set())
            trigger = interrupt_action.trigger
            trigger_deadline = (
                recorder.clock.now().timestamp_monotonic_ns + trigger.timeout_ms * 1_000_000
            )
            matched = await wait_event(
                lambda e: _trigger_matches(e, trigger) and e.response_id is not None,
                trigger_deadline,
                "trigger_timeout",
                trigger.occurrence,
            )
            target_response_id = matched.response_id
            stop_tail.set()
            await first_sender  # Never interleave t1 and t2 PCM on the same input stream.
            deadline_ns = matched.timestamp_monotonic_ns + trigger.delay_ms * 1_000_000
            await asyncio.sleep(
                max(0, (deadline_ns - recorder.clock.now().timestamp_monotonic_ns) / 1e9)
            )
            if (
                recorder.clock.now().timestamp_monotonic_ns - deadline_ns
                > profile.max_send_lateness_ms * 1e6
            ):
                raise TimingViolation("interruption_trigger_deadline_missed")
            check_transport()
            stimulus_evidence = preconditions()
            if not stimulus_evidence["valid"]:
                raise InterruptionExecutionError("interruption_precondition_failed")
            publish(
                EventDraft(
                    **context.model_dump(),
                    **recorder.clock.now().model_dump(),
                    event="scenario_action_start",
                    source="system",
                    producer="scenario.engine",
                    timing={"basis": "simulator_boundary"},
                    turn_id=interrupt_action.turn_id,
                    response_id=target_response_id,
                    causal_event_id=matched.event_id,
                    payload={
                        "action_id": interrupt_action.action_id,
                        "target_response_id": target_response_id,
                    },
                )
            )
            await send_utterance(
                adapter,
                sink,
                context,
                recorder.clock,
                publish,
                action=interrupt_action,
                asset=interrupt_asset,
                pcm=read_wav_bytes(source_wavs[interrupt_asset.path], config.input_audio),
                chunk_ms=scenario.audio.chunk_ms,
                profile=profile,
            )
            if not stimulus_evidence["valid"]:
                raise InterruptionExecutionError("interruption_precondition_failed_at_speech_start")
            input_end = next(
                e
                for e in observed
                if e.event == "user_audio_end" and e.turn_id == interrupt_action.turn_id
            )
            deadline_ns = (
                input_end.timestamp_monotonic_ns
                + scenario.termination.post_stimulus_observation_ms * 1_000_000
            )
            await wait_event(
                lambda e: (
                    e.event == "assistant_response_end" and e.response_id == target_response_id
                ),
                deadline_ns,
                "old_response_timeout",
            )
            if is_backchannel:
                old_end = next(
                    e
                    for e in observed
                    if e.event == "assistant_response_end" and e.response_id == target_response_id
                )
                if old_end.payload.status != "completed":
                    status, reason = "model_failed", "old_response_cancelled_after_backchannel"
            else:
                marker = next(e for e in observed if e.event == marker_kind)
                new_start = await wait_event(
                    lambda e: (
                        e.event == "assistant_response_start"
                        and e.response_id != target_response_id
                        and e.turn_id == interrupt_action.turn_id
                        and e.timestamp_monotonic_ns >= marker.timestamp_monotonic_ns
                    ),
                    deadline_ns,
                    "new_response_timeout",
                )
                new_end = await wait_event(
                    lambda e: (
                        e.event == "assistant_response_end"
                        and e.response_id == new_start.response_id
                    ),
                    deadline_ns,
                    "new_response_completion_timeout",
                )
                if new_end.payload.status != "completed":
                    status, reason = "model_failed", "new_response_not_completed"
                elif not any(
                    e.event == "assistant_audio_chunk" and e.response_id == new_start.response_id
                    for e in observed
                ):
                    status, reason = "model_failed", "new_response_no_audio"
    except UnsupportedCapability:
        status, reason = "unsupported", "required_capability_unavailable"
    except TimingViolation as error:
        status, reason = "invalid", str(error)
    except InterruptionExecutionError as error:
        status, reason = "invalid", str(error)
    except TimeoutError as error:
        status, reason = "model_failed", str(error) or "case_timeout"
    except (Exception, asyncio.CancelledError) as error:
        status, reason = "infra_failed", type(error).__name__
    finally:
        if interrupt_marker_written:
            publish(
                EventDraft(
                    **context.model_dump(),
                    **recorder.clock.now().model_dump(),
                    event="scenario_action_end",
                    source="system",
                    producer="scenario.engine",
                    timing={"basis": "simulator_boundary"},
                    turn_id=interrupt_action.turn_id,
                    response_id=target_response_id,
                    payload={
                        "action_id": interrupt_action.action_id,
                        "target_response_id": target_response_id,
                        "timed_out": "timeout" in reason,
                        "reason": reason,
                    },
                )
            )
        closing = True
        if first_sender:
            if not first_sender.done():
                first_sender.cancel()
            await asyncio.gather(first_sender, return_exceptions=True)
        if adapter:
            try:
                await adapter.close()
            except Exception:
                cleanup_warnings.append("adapter_close_failed")
        if collector:
            try:
                await asyncio.wait_for(collector, 5)
            except Exception:
                status, reason = "infra_failed", "event_collection_failed"
        try:
            await asyncio.wait_for(playback.finish(), scenario.termination.drain_timeout_ms / 1000)
        except TimeoutError:
            cleanup_warnings.append("playback_drain_timeout_truncated")
            try:
                await playback.abort()
            except Exception:
                status, reason = "invalid", "playback_abort_failed"
        except Exception as error:
            status, reason = "invalid", str(error) or type(error).__name__

    old_response_id = next(
        (event.response_id for event in observed if event.event == marker_kind), None
    )
    interrupted = [
        event
        for event in observed
        if event.event == "interrupt_detected" and event.response_id == old_response_id
    ]
    old_stop = next(
        (
            event
            for event in observed
            if event.event == "assistant_playback_stop" and event.response_id == old_response_id
        ),
        None,
    )
    interrupt_event = next((event for event in observed if event.event == marker_kind), None)
    new_response = next(
        (
            event
            for event in observed
            if interrupt_event
            and event.event == "assistant_response_start"
            and event.response_id != old_response_id
            and event.turn_id == interrupt_action.turn_id
            and event.timestamp_monotonic_ns >= interrupt_event.timestamp_monotonic_ns
        ),
        None,
    )
    trial = {
        "status": status,
        "reason": reason,
        "warmup": warmup,
        "old_response_id": old_response_id,
        "new_response_id": new_response.response_id if new_response else None,
        "interruption_detected": bool(interrupted),
        "detection_evidence_level": interrupted[0].payload.evidence_level if interrupted else None,
        "interrupt_start_event_id": interrupt_event.event_id if interrupt_event else None,
        "old_playback_stop_event_id": old_stop.event_id if old_stop else None,
        "stop_latency_ms": (
            (old_stop.timestamp_monotonic_ns - interrupt_event.timestamp_monotonic_ns) / 1e6
            if old_stop and interrupt_event
            else None
        ),
        "residual_audio_duration_ms": _residual_audio_ms(
            observed,
            old_response_id,
            interrupt_event.timestamp_monotonic_ns if interrupt_event else None,
            old_stop.timestamp_monotonic_ns if old_stop else None,
        ),
        "cleanup_warnings": cleanup_warnings
        + (
            ["session_close_not_acknowledged"]
            if any(
                event.event == "session_end" and not event.payload.complete for event in observed
            )
            else []
        ),
        "backend": adapter.diagnostics() if adapter else {},
        "capabilities": adapter.capabilities().model_dump(mode="json") if adapter else {},
        "artifact_queue": sink.diagnostics(),
        "stimulus_evidence": stimulus_evidence,
    }
    if is_backchannel:
        # These interruption diagnostics have no meaning for a listener stimulus.
        for key in (
            "interruption_detected",
            "detection_evidence_level",
            "interrupt_start_event_id",
            "stop_latency_ms",
            "residual_audio_duration_ms",
        ):
            trial.pop(key, None)
        trial["backchannel_start_event_id"] = interrupt_event.event_id if interrupt_event else None
    case_end = EventDraft(
        **context.model_dump(),
        **recorder.clock.now().model_dump(),
        event="case_end",
        source="system",
        producer="benchmark.interruption",
        timing={"basis": "inferred"},
        payload={"status": status, "reason": reason},
    )
    try:
        publish(case_end)
        await sink.finish()
    except ArtifactBackpressure:
        status, reason = "infra_failed", "artifact_write_failed"
        trial.update(status=status, reason=reason)
        try:
            await sink.finish()
        except ArtifactBackpressure:
            pass
        await recorder.write_json("trial.json", trial)
        await recorder.close(complete=False, reason=reason)
        return trial
    input_events = [event for event in observed if event.event == "user_audio_chunk"]
    output_events = [event for event in observed if event.event == "assistant_audio_chunk"]
    input_pcm = b"".join(sink.audio_part(event.payload.audio_ref) for event in input_events)
    output_pcm = b"".join(sink.audio(event.payload.audio_ref) for event in output_events)
    origin = next(
        (event.timestamp_monotonic_ns for event in observed if event.event == "session_start"),
        recorder.origin.timestamp_monotonic_ns,
    )
    timeline = render_timeline(playback.segments, origin_ns=origin, format=config.output_audio)
    await recorder.store_wav("input.wav", input_pcm, config.input_audio)
    await recorder.store_wav("output_received.wav", output_pcm, config.output_audio)
    await recorder.store_wav("output.wav", timeline, config.output_audio)
    await recorder.write_json(
        "session_config.json", ack.model_dump(mode="json") if ack else {"status": "unavailable"}
    )
    await recorder.write_json(
        "transcript.json",
        [
            {
                "event": event.event,
                "event_id": event.event_id,
                "turn_id": event.turn_id,
                "response_id": event.response_id,
                **event.payload.model_dump(mode="json"),
            }
            for event in observed
            if event.event in {"user_text_done", "assistant_text_done"}
        ],
    )
    await recorder.write_json("trial.json", trial)
    has_start = any(event.event == "session_start" for event in observed)
    has_end = any(event.event == "session_end" for event in observed)
    await recorder.close(
        complete=has_start and has_end, reason=None if has_start and has_end else reason
    )
    return trial


def _residual_audio_ms(
    events: list[EventDraft],
    response_id: str | None,
    interrupt_timestamp_ns: int | None = None,
    stop_timestamp_ns: int | None = None,
) -> float | None:
    if not response_id or interrupt_timestamp_ns is None:
        return None
    chunks = [
        event
        for event in events
        if event.event == "assistant_playback_chunk" and event.response_id == response_id
    ]
    if not chunks:
        return 0.0
    residual_ns = 0
    for chunk in chunks:
        start = chunk.timestamp_monotonic_ns
        end = start + chunk.payload.sample_count * 1_000_000_000 // chunk.payload.sample_rate_hz
        residual_ns += max(
            0, min(end, stop_timestamp_ns or end) - max(start, interrupt_timestamp_ns)
        )
    return residual_ns / 1e6
