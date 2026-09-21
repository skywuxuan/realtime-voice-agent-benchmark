import asyncio
import copy
from datetime import UTC, datetime

import pytest

from adapters.base import SessionConfig
from adapters.testing import ScriptedAdapter
from benchmark.audio import AudioFormat, AudioRef
from benchmark.config import LatencyProfile
from benchmark.contracts import content_hash
from benchmark.evaluate import evaluate_run
from benchmark.run import run_suite
from evaluator.realtime import aggregate, distribution, evaluate_case
from events.recorder import EventRecorder
from events.schema import EventDraft
from scenarios.schema import Scenario
from simulator.audio import render_timeline


def config(mode="manual"):
    return SessionConfig(
        model="fixture",
        voice="fixture",
        turn_mode=mode,
        input_audio=AudioFormat(sample_rate_hz=16000),
        output_audio=AudioFormat(sample_rate_hz=24000),
    )


async def metric_record(
    root,
    scenario_data,
    context,
    clock,
    *,
    first_audio_ms=250,
    status="completed",
    reason="response_completed",
    warmup=False,
    lateness_ms=0,
    mode="manual",
    annotation="fixture",
    close_complete=True,
):
    scenario = copy.deepcopy(scenario_data)
    asset = scenario["audio"]["assets"]["question"]
    asset["speech_bounds_samples"] = [0, 320]  # 20 ms speech, 80 ms frozen trailing silence.
    asset["boundary_annotation"] = {
        "method": annotation,
        "status": "synthetic" if annotation == "fixture" else "automatic",
        "resolution_ms": 20,
        "parameters": {},
    }
    model = config(mode)
    async with EventRecorder(
        root,
        context,
        clock=clock,
        config={
            "mode": "latency_benchmark",
            "warmup": warmup,
            "scenario_sha256": "fixture",
            "model_config": model.model_dump(mode="json"),
            "model_config_sha256": content_hash(model.model_dump(mode="json")),
            "latency_profile": LatencyProfile().model_dump(mode="json"),
            "playback_mode": "virtual",
            "boundary_annotation": asset["boundary_annotation"],
            "input_kind": "synthetic_fixture",
        },
    ) as recorder:
        await recorder.write_json("scenario.json", scenario)
        await recorder.write_json("trial.json", {"status": status, "reason": reason})

        def draft(kind, ms, payload, **fields):
            return EventDraft(
                **context.model_dump(),
                clock_id=clock.now().clock_id,
                timestamp_monotonic_ns=1_000_000_000 + int(ms * 1e6),
                wall_clock_timestamp=datetime(2026, 9, 17, tzinfo=UTC),
                event=kind,
                source="system",
                producer="fixture",
                timing={"basis": "client_receive"},
                payload=payload,
                **fields,
            )

        await recorder.record(
            draft(
                "session_start",
                0,
                {"vendor_session_id": None, "adapter_version": "fixture", "capabilities": {}},
            )
        )
        ref = await recorder.store_audio("input", b"\1\0" * 1600, model.input_audio)
        for index in range(5):
            part = AudioRef(
                **model.input_audio.model_dump(),
                path=ref.path,
                byte_offset=index * 640,
                byte_length=640,
                sample_offset=index * 320,
                sample_count=320,
            )
            end_ms = (index + 1) * 20
            chunk = draft(
                "user_audio_chunk",
                end_ms,
                {
                    "audio_ref": part.model_dump(),
                    "chunk_index": index,
                    "planned_send_ns": int(1e9 + end_ms * 1e6),
                    "send_started_ns": int(1e9 + (end_ms + lateness_ms) * 1e6),
                    "send_completed_ns": int(1e9 + (end_ms + lateness_ms) * 1e6),
                    "silence": index > 0,
                },
                turn_id="t1",
                stream_id="input",
            )
            await recorder.record(chunk)
            if index == 0:
                await recorder.record(
                    draft(
                        "user_audio_end",
                        end_ms + lateness_ms,
                        {"action_id": "ask", "end_sample": 320, "annotation_source": annotation},
                        turn_id="t1",
                        stream_id="input",
                        causal_event_id=chunk.event_id,
                    )
                )
        if mode == "manual":
            await recorder.record(
                draft("user_turn_commit", 100, {"phase": "requested"}, turn_id="t1")
            )
        if first_audio_ms is not None:
            await recorder.record(
                draft(
                    "assistant_response_start",
                    first_audio_ms - 1,
                    {
                        "response_status": "in_progress",
                        "trigger_turn_id": "t1",
                        "association_method": "inferred_serial_turn",
                    },
                    response_id="r1",
                    turn_id="t1",
                )
            )
            audio = await recorder.store_audio("r1", b"\1\0" * 480, model.output_audio)
            chunk = draft(
                "assistant_audio_chunk",
                first_audio_ms,
                {"audio_ref": audio.model_dump(), "chunk_index": 0},
                response_id="r1",
                turn_id="t1",
                stream_id="r1",
            )
            await recorder.record(
                draft(
                    "assistant_audio_start",
                    first_audio_ms,
                    {
                        "first_chunk_event_id": chunk.event_id,
                        "audio_format": model.output_audio.model_dump(),
                    },
                    response_id="r1",
                    turn_id="t1",
                )
            )
            await recorder.record(chunk)
            await recorder.record(
                draft(
                    "assistant_playback_start",
                    first_audio_ms + 5,
                    {"sample_offset": 0, "sample_count": 0, "sample_rate_hz": 24000},
                    response_id="r1",
                    turn_id="t1",
                )
            )
        end_ms = 16000 if reason == "response_timeout" else 1000
        await recorder.record(
            draft(
                "session_end",
                end_ms,
                {"reason": "client_websocket_close", "complete": close_complete},
            )
        )
        await recorder.record(draft("case_end", end_ms + 1, {"status": status, "reason": reason}))


def test_latency_uses_annotated_speech_end_not_file_end(tmp_path, scenario_data, context, clock):
    root = tmp_path / "case"
    asyncio.run(metric_record(root, scenario_data, context, clock))
    result = evaluate_case(root)
    assert result["status"] == "pass"
    assert result["ttfa_receive_ms"] == 230  # 250 - 20, never 250 - file_end(100).
    assert result["ttfa_playback_ms"] == 235
    assert result["commit_to_audio_ms"] == 150
    assert result["evidence"]["user_audio_end"]


@pytest.mark.parametrize(
    "kind,expected", [("timeout", "fail"), ("premature", "fail"), ("jitter", "invalid")]
)
def test_failures_and_negative_latencies_are_not_turned_into_zero(
    tmp_path, scenario_data, context, clock, kind, expected
):
    kwargs = (
        {"first_audio_ms": None, "status": "model_failed", "reason": "response_timeout"}
        if kind == "timeout"
        else ({"first_audio_ms": 10} if kind == "premature" else {"lateness_ms": 25})
    )
    root = tmp_path / "case"
    asyncio.run(metric_record(root, scenario_data, context, clock, **kwargs))
    result = evaluate_case(root)
    assert result["status"] == expected
    stats = aggregate([result])
    group = stats["realtime"]["groups"][0]
    assert group["ttfa_receive_ms"]["n"] == 0
    assert group["ttfa_receive_ms"]["p50"] is None
    if kind == "premature":
        assert result["ttfa_receive_ms"] == -10
        assert result["premature"]
    if kind == "timeout":
        assert result["censored"] and group["counts"]["timeout"] == 1


def test_grouping_warmup_and_nearest_rank(tmp_path, scenario_data, context, clock):
    cases = []
    for index, (mode, annotation, warmup) in enumerate(
        [
            ("manual", "fixture", False),
            ("server_vad", "fixture", False),
            ("manual", "energy_rms_v1", False),
            ("manual", "fixture", True),
        ]
    ):
        root = tmp_path / str(index)
        asyncio.run(
            metric_record(
                root, scenario_data, context, clock, mode=mode, annotation=annotation, warmup=warmup
            )
        )
        cases.append(evaluate_case(root))
    summary = aggregate(cases)
    assert len(summary["realtime"]["groups"]) == 3
    assert summary["realtime"]["counts"]["attempted"] == 3
    assert summary["realtime"]["warmup_count"] == 1
    values = distribution(list(range(1, 11)), eligible_count=11, timeout_count=1)
    assert (values["p50"], values["p90"], values["p95"], values["p99"], values["mean"]) == (
        5,
        9,
        10,
        10,
        5.5,
    )
    assert values["eligible_count"] == 11 and values["timeout_count"] == 1


def test_corrupt_artifacts_cannot_produce_a_latency_score(tmp_path, scenario_data, context, clock):
    root = tmp_path / "case"
    asyncio.run(metric_record(root, scenario_data, context, clock))
    with (root / "events.jsonl").open("ab") as stream:
        stream.write(b"broken")
    assert evaluate_case(root)["status"] == "invalid"


@pytest.mark.parametrize("timeout", [True, False])
def test_cleanup_handshake_does_not_erase_observed_latency_or_timeout(
    tmp_path, scenario_data, context, clock, timeout
):
    root = tmp_path / "case"
    options = (
        {"first_audio_ms": None, "status": "model_failed", "reason": "response_timeout"}
        if timeout
        else {}
    )
    asyncio.run(metric_record(root, scenario_data, context, clock, close_complete=False, **options))
    result = evaluate_case(root)
    assert result["status"] == ("fail" if timeout else "pass")
    assert result["cleanup_warnings"] == ["session_close_not_acknowledged"]
    summary = aggregate([result])["realtime"]
    assert summary["counts"]["timeout"] == (1 if timeout else 0)
    assert summary["groups"][0]["ttfa_receive_ms"]["n"] == (0 if timeout else 1)


def test_virtual_timeline_preserves_silence_and_rejects_overlap():
    fmt = AudioFormat(sample_rate_hz=1000)
    assert (
        render_timeline([(2_000_000, b"\1\0"), (5_000_000, b"\2\0")], origin_ns=0, format=fmt)
        == b"\0\0" * 2 + b"\1\0" + b"\0\0" * 2 + b"\2\0"
    )
    with pytest.raises(ValueError, match="overlap"):
        render_timeline([(0, b"\1\0" * 2), (1_000_000, b"\2\0")], origin_ns=0, format=fmt)


@pytest.mark.parametrize("emit_audio", [True, False])
def test_runner_and_offline_re_evaluation_need_no_credentials(
    tmp_path, scenario_data, monkeypatch, emit_audio
):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    scenario_data["audio"]["assets"]["question"]["boundary_annotation"] = {
        "method": "fixture",
        "status": "synthetic",
        "resolution_ms": 20,
    }
    scenario = Scenario.model_validate(scenario_data)

    class RespondingAdapter(ScriptedAdapter):
        def __init__(self, context, sink, clock):
            super().__init__(context, clock)
            self.sink = sink

        def build(self, kind, payload, *, reading=None, **fields):
            e = EventDraft(
                **self.context.model_dump(),
                **(reading or self.clock.now()).model_dump(),
                event=kind,
                source="system",
                producer="fixture",
                timing={"basis": "client_receive"},
                payload=payload,
                **fields,
            )
            return e

        def emit(self, kind, payload, **fields):
            e = self.build(kind, payload, **fields)
            self.queue.put_nowait(e)
            return e

        async def _connect(self):
            self.emit(
                "session_start",
                {"vendor_session_id": None, "adapter_version": "fixture", "capabilities": {}},
            )
            return await super()._connect()

        async def _commit_turn(self, turn_id):
            self.emit(
                "assistant_response_start",
                {
                    "response_status": "in_progress",
                    "trigger_turn_id": turn_id,
                    "association_method": "inferred_serial_turn",
                },
                response_id="r1",
                turn_id=turn_id,
            )
            if not emit_audio:
                self.emit(
                    "assistant_response_end",
                    {"status": "completed", "completion_source": "fixture"},
                    response_id="r1",
                    turn_id=turn_id,
                )
                return
            ref = await self.sink.store_audio("r1", b"\1\0" * 480, self.config.output_audio)
            reading = self.clock.now()
            chunk = self.build(
                "assistant_audio_chunk",
                {"audio_ref": ref.model_dump(), "chunk_index": 0},
                response_id="r1",
                turn_id=turn_id,
                stream_id="r1",
                reading=reading,
            )
            self.emit(
                "assistant_audio_start",
                {
                    "first_chunk_event_id": chunk.event_id,
                    "audio_format": self.config.output_audio.model_dump(),
                },
                response_id="r1",
                turn_id=turn_id,
                reading=reading,
            )
            self.queue.put_nowait(chunk)
            self.emit(
                "assistant_audio_end",
                {
                    "reason": "completed",
                    "last_chunk_event_id": chunk.event_id,
                    "complete": True,
                    "completion_source": "fixture",
                },
                response_id="r1",
                turn_id=turn_id,
            )
            self.emit(
                "assistant_response_end",
                {"status": "completed", "completion_source": "fixture"},
                response_id="r1",
                turn_id=turn_id,
            )

        async def _close(self):
            self.emit("session_end", {"reason": "closed", "complete": True})
            await super()._close()

    class Registration:
        alias = "fixture"

        def factory(self, config):
            return RespondingAdapter

    async def run():
        result = await run_suite(
            output=tmp_path / "run",
            scenarios=(scenario,),
            source_root=tmp_path,
            registration=Registration(),
            model_config=config(),
            profile=LatencyProfile(),
            warmups=1,
        )
        assert result["realtime"]["counts"]["pass" if emit_audio else "fail"] == 1
        if not emit_audio:
            assert result["realtime"]["counts"]["timeout"] == 0
        assert result["realtime"]["warmup_count"] == 1
        before = (tmp_path / "run" / "metrics.json").read_bytes()
        again = evaluate_run(tmp_path / "run")
        assert again == result and (tmp_path / "run" / "metrics.json").read_bytes() == before
        path = tmp_path / "run" / "cases" / scenario.scenario_id / "attempt_001"
        assert (path / "output.wav").is_file()

    asyncio.run(run())
