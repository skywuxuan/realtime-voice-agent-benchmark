"""Voice Agent execution. Only observed adapter tool events can drive Mock Tools."""

import asyncio
import hashlib
from pathlib import Path

from adapters.base import SessionConfig, UnsupportedCapability
from benchmark.config import LatencyProfile
from events.buffered import ArtifactBackpressure, BufferedArtifacts
from events.clock import SystemClock
from events.recorder import EventRecorder
from events.schema import EventDraft, RecordingContext
from scenarios.schema import PlayAudio
from simulator.audio import read_wav_bytes, render_timeline
from simulator.input import TimingViolation, send_utterance
from simulator.playback import VirtualPlayback
from tools.catalog import ProtocolToolServer, ToolCatalog
from tools.definitions import definitions
from tools.runtime import ToolRuntime
from tools.scenarios import AgentScenario, AgentTurnTrigger
from tools.server import FailureFixture, MockToolServer


def public_config(
    scenario: AgentScenario, config: SessionConfig, tool_catalog: ToolCatalog | None = None
) -> SessionConfig:
    # Explicit allowlist: answer oracles, expected calls and state never reach the adapter.
    if scenario.tool_backend == "protocol_ack_v1":
        if tool_catalog is None:
            raise ValueError("external tool catalog is required")
        exposed_tools = tool_catalog.definitions(scenario.tools_enabled)
    else:
        if tool_catalog is not None:
            raise ValueError("fixed mock tools cannot use an external catalog")
        exposed_tools = definitions(scenario.tools_enabled)
    return config.model_copy(
        update={
            "tools": exposed_tools,
            "system_prompt": scenario.system_prompt
            + "\n固定场景时间："
            + str(scenario.world.get("now", "未指定")),
        }
    )


async def run_agent_case(
    factory,
    *,
    scenario: AgentScenario,
    source_wavs: dict[str, bytes],
    output: Path,
    context: RecordingContext,
    config: SessionConfig,
    profile: LatencyProfile,
    secrets=(),
    implementation_hash="unavailable",
    warmup=False,
    tool_catalog: ToolCatalog | None = None,
):
    if len(scenario.turn_assets) != len(scenario.user_turns):
        raise ValueError(
            "each user turn requires a frozen audio asset; oracle calls are never executed"
        )
    assets = [scenario.audio_assets[name] for name in scenario.turn_assets]
    for asset in assets:
        if hashlib.sha256(source_wavs[asset.path]).hexdigest() != asset.sha256:
            raise ValueError("frozen agent audio hash mismatch")
        pcm = read_wav_bytes(source_wavs[asset.path], config.input_audio)
        if asset.speech_bounds_samples[1] > len(pcm) // config.input_audio.bytes_per_sample_frame:
            raise ValueError("speech bounds extend beyond agent audio")
    if scenario.tool_backend == "protocol_ack_v1":
        if tool_catalog is None or tool_catalog.catalog_id != scenario.tool_catalog.catalog_id:
            raise ValueError("scenario tool catalog is unavailable or mismatched")
        server = ProtocolToolServer(tool_catalog, enabled=scenario.tools_enabled)
    else:
        if set(scenario.initial_state) - {"calendar_events"}:
            raise ValueError("unsupported initial state fields")
        server = MockToolServer(
            failures=tuple(FailureFixture(**row) for row in scenario.failure_schedule),
            calendar_events=scenario.initial_state.get("calendar_events", []),
            enabled=scenario.tools_enabled,
        )
    config = public_config(scenario, config, tool_catalog)
    recorder = EventRecorder(
        output,
        context,
        clock=SystemClock(),
        secrets=secrets,
        config={
            "mode": "agent_benchmark",
            "warmup": warmup,
            "scenario_sha256": scenario.sha256,
            "model_config": config.model_dump(mode="json"),
            "profile": profile.model_dump(mode="json"),
            "input_chunk_ms": scenario.input_chunk_ms,
            "implementation_sha256": implementation_hash,
            "execution_source": "adapter_events",
            "playback_mode": "virtual",
            "tool_backend": scenario.tool_backend,
            "tool_catalog_id": tool_catalog.catalog_id if tool_catalog else None,
        },
    )
    await recorder.__aenter__()
    await recorder.write_json("scenario.json", scenario.model_dump(mode="json"))
    if tool_catalog is not None:
        await recorder.write_json("tool_catalog.json", tool_catalog.model_dump(mode="json"))
    for asset in assets:
        await recorder.store_blob(source_wavs[asset.path])
    sink = BufferedArtifacts(
        recorder, capacity=profile.artifact_queue_capacity, max_bytes=profile.artifact_max_bytes
    )
    observed, handled = [], {}
    changed = asyncio.Event()
    terminals = {}
    workers = []
    dispatcher = None
    stop_tails = {}
    completed = asyncio.Queue()
    fatal = asyncio.Event()
    closing = False
    adapter = collector = None
    ack = None
    cleanup = []
    playback = VirtualPlayback(context, recorder.clock, sink, lambda e: None, profile)

    def publish(event):
        sink.emit(event)
        observed.append(event)
        changed.set()
        if event.event == "assistant_response_start" and event.turn_id in stop_tails:
            stop_tails[event.turn_id].set()
        if event.event in {"assistant_audio_chunk", "assistant_audio_end", "assistant_cancelled"}:
            playback.submit(event)
        if event.event == "assistant_response_end":
            completed.put_nowait(event)
        if event.event == "error" and event.payload.fatal:
            fatal.set()
            completed.put_nowait(None)

    playback.publish = publish
    runtime = ToolRuntime(server, context, recorder.clock, publish)

    async def collect():
        try:
            while True:
                publish(await adapter.receive_event())
        except EOFError:
            if not closing:
                fatal.set()
                completed.put_nowait(None)
        except Exception:
            fatal.set()
            completed.put_nowait(None)
            raise

    def worker_done(task):
        if not task.cancelled() and task.exception() is not None:
            fatal.set()
            completed.put_nowait(None)
        changed.set()

    async def execute_batch(calls):
        for call in calls:
            args = call.payload.arguments if call.payload.valid_json else {"invalid_json": True}
            result = await runtime.execute_async(
                call.payload.name,
                args,
                call_id=call.call_id,
                response_id=call.response_id,
                delays=scenario.tool_delays,
            )
            await adapter.send_tool_result(result)

    async def dispatch():
        while True:
            end = await completed.get()
            if end is None or fatal.is_set():
                raise ConnectionError("adapter_failed")
            if end.turn_id is None:
                raise ValueError("ambiguous_agent_response_association")
            calls = [
                e
                for e in observed
                if e.event == "tool_call_end" and e.response_id == end.response_id
            ]
            if not calls or end.payload.status != "completed":
                terminals[end.turn_id] = end
                changed.set()
                continue
            fresh = []
            for call in calls:
                signature = (call.response_id, call.payload.model_dump())
                if call.call_id in handled:
                    if signature != handled[call.call_id]:
                        raise ValueError("conflicting_tool_call")
                    continue
                if len(handled) >= scenario.max_tool_calls:
                    raise ValueError("tool_call_limit_exceeded")
                handled[call.call_id] = signature
                fresh.append(call)
            if fresh:
                task = asyncio.create_task(execute_batch(fresh))
                workers.append(task)
                task.add_done_callback(worker_done)

    async def wait_for(check, timeout):
        async with asyncio.timeout(timeout):
            while True:
                if fatal.is_set():
                    raise ConnectionError("adapter_or_tool_dispatch_failed")
                result = check()
                if result:
                    return result
                changed.clear()
                await changed.wait()

    def trigger_event(trigger, previous_turn):
        matching_calls = {
            e.call_id
            for e in observed
            if e.event == "tool_call_end"
            and e.turn_id == previous_turn
            and e.payload.name == trigger.tool
        }
        matches = [
            e for e in observed if e.event == "tool_execution_start" and e.call_id in matching_calls
        ]
        return matches[trigger.occurrence - 1] if len(matches) >= trigger.occurrence else None

    status, reason = "completed", "response_completed"
    try:
        async with asyncio.timeout(scenario.response_timeout_s * (len(assets) + 1)):
            adapter = factory(context, sink, clock=recorder.clock)
            adapter.capabilities().require(
                "audio_input", "audio_output", "tool_calling", "tool_result_injection"
            )
            await adapter.connect()
            collector = asyncio.create_task(collect())
            ack = await adapter.configure(config)
            dispatcher = asyncio.create_task(dispatch())
            dispatcher.add_done_callback(worker_done)
            for index, asset in enumerate(assets, 1):
                turn_id = f"t{index}"
                cause = None
                if index > 1:
                    trigger = (
                        scenario.turn_triggers[index - 2]
                        if scenario.turn_triggers
                        else AgentTurnTrigger()
                    )
                    if trigger.type == "after_tool_start":
                        cause = await wait_for(
                            lambda: trigger_event(trigger, f"t{index - 1}"),
                            scenario.response_timeout_s,
                        )
                        target_ns = cause.timestamp_monotonic_ns + trigger.delay_ms * 1_000_000
                        await asyncio.sleep(
                            max(0, (target_ns - recorder.clock.now().timestamp_monotonic_ns) / 1e9)
                        )
                        if (
                            recorder.clock.now().timestamp_monotonic_ns - target_ns
                            > profile.max_send_lateness_ms * 1e6
                        ):
                            raise TimingViolation("agent_trigger_deadline_missed")
                        if any(
                            e.event == "tool_execution_end" and e.call_id == cause.call_id
                            for e in observed
                        ):
                            raise TimingViolation("correction_tool_not_pending")
                    else:
                        previous = await wait_for(
                            lambda: terminals.get(f"t{index - 1}"), scenario.response_timeout_s
                        )
                        if previous.payload.status != "completed":
                            status, reason = "model_failed", "previous_response_not_completed"
                            break
                        cause = await wait_for(
                            lambda: next(
                                (
                                    e
                                    for e in observed
                                    if e.event == "assistant_playback_stop"
                                    and e.response_id == previous.response_id
                                    and e.payload.stop_reason == "completed"
                                ),
                                None,
                            ),
                            scenario.drain_timeout_s,
                        )
                publish(
                    EventDraft(
                        **context.model_dump(),
                        **recorder.clock.now().model_dump(),
                        event="scenario_action_start",
                        source="system",
                        producer="agent.runtime",
                        timing={"basis": "simulator_boundary"},
                        turn_id=turn_id,
                        response_id=cause.response_id if cause else None,
                        causal_event_id=cause.event_id if cause else None,
                        payload={
                            "action_id": f"ask_{index}",
                            "target_response_id": cause.response_id if cause else None,
                        },
                    )
                )
                stop_tails[turn_id] = asyncio.Event()
                await send_utterance(
                    adapter,
                    sink,
                    context,
                    recorder.clock,
                    publish,
                    action=PlayAudio(
                        action_id=f"ask_{index}",
                        type="play_audio",
                        asset=scenario.turn_assets[index - 1],
                        turn_id=turn_id,
                        trigger={"type": "session_ready"},
                    ),
                    asset=asset,
                    pcm=read_wav_bytes(source_wavs[asset.path], config.input_audio),
                    chunk_ms=scenario.input_chunk_ms,
                    profile=profile,
                    stop_tail=stop_tails[turn_id],
                )
            if status == "completed":
                end = await wait_for(
                    lambda: terminals.get(f"t{len(assets)}"), scenario.response_timeout_s
                )
                if end.payload.status != "completed":
                    status, reason = "model_failed", "response_not_completed"
                elif not any(
                    e.event == "assistant_audio_chunk" and e.response_id == end.response_id
                    for e in observed
                ):
                    status, reason = "model_failed", "terminal_response_no_audio"
                if workers:
                    await asyncio.gather(*workers)
    except UnsupportedCapability:
        status, reason = "unsupported", "agent_tool_capabilities_unavailable"
    except TimingViolation as error:
        status, reason = "invalid", str(error)
    except TimeoutError:
        status, reason = "model_failed", "agent_response_timeout"
    except (Exception, asyncio.CancelledError) as error:
        status = "infra_failed"
        reason = getattr(error, "code", type(error).__name__)
    finally:
        closing = True
        if dispatcher:
            dispatcher.cancel()
            await asyncio.gather(dispatcher, return_exceptions=True)
        for task in workers:
            if not task.done():
                task.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        if adapter:
            try:
                await adapter.close()
            except Exception:
                cleanup.append("adapter_close_failed")
        if collector:
            try:
                await asyncio.wait_for(collector, 5)
            except Exception:
                status, reason = "infra_failed", "event_collection_failed"
        try:
            await asyncio.wait_for(playback.finish(), scenario.drain_timeout_s)
        except TimeoutError:
            cleanup.append("playback_drain_timeout_truncated")
            await playback.abort()
        except Exception:
            status, reason = "invalid", "playback_failed"

    trial = {
        "status": status,
        "reason": reason,
        "cleanup_warnings": cleanup,
        "execution_source": "adapter_events",
        "tool_calls_executed": len(server.records),
        "completion_claim_status": "unknown",
        "backend": adapter.diagnostics() if adapter else {},
    }
    try:
        publish(
            EventDraft(
                **context.model_dump(),
                **recorder.clock.now().model_dump(),
                event="case_end",
                source="system",
                producer="agent.runtime",
                timing={"basis": "inferred"},
                payload={"status": status, "reason": reason},
            )
        )
        await sink.finish()
    except ArtifactBackpressure:
        trial.update(status="infra_failed", reason="artifact_write_failed")
        await recorder.write_json("trial.json", trial)
        await recorder.close(complete=False, reason="artifact_write_failed")
        return trial
    origin = next(
        (e.timestamp_monotonic_ns for e in observed if e.event == "session_start"),
        recorder.origin.timestamp_monotonic_ns,
    )
    await recorder.store_wav(
        "input.wav",
        b"".join(
            sink.audio_part(e.payload.audio_ref) for e in observed if e.event == "user_audio_chunk"
        ),
        config.input_audio,
    )
    await recorder.store_wav(
        "output_received.wav",
        b"".join(
            sink.audio(e.payload.audio_ref) for e in observed if e.event == "assistant_audio_chunk"
        ),
        config.output_audio,
    )
    await recorder.store_wav(
        "output.wav",
        render_timeline(playback.segments, origin_ns=origin, format=config.output_audio),
        config.output_audio,
    )
    await recorder.write_json(
        "transcript.json",
        [
            e.model_dump(mode="json")
            for e in observed
            if e.event in {"assistant_text_done", "user_text_done"}
        ],
    )
    await recorder.write_json(
        "session_config.json", ack.model_dump(mode="json") if ack else {"status": "unavailable"}
    )
    await recorder.write_json("tool_calls.json", server.trace())
    await recorder.write_json(
        "tool_results.json",
        [e.payload.model_dump(mode="json") for e in observed if e.event == "tool_result"],
    )
    await recorder.write_json("state.json", server.state())
    await recorder.write_json("trial.json", trial)
    complete = any(e.event == "session_start" for e in observed) and any(
        e.event == "session_end" for e in observed
    )
    await recorder.close(complete=complete, reason=None if complete else reason)
    return trial
