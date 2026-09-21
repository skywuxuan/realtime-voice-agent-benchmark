import asyncio
import hashlib
from copy import deepcopy

import pytest

from adapters.base import SessionConfig
from adapters.testing import ScriptedAdapter
from benchmark.audio import AudioFormat, AudioRef
from benchmark.config import LatencyProfile
from benchmark.evaluate import evaluate_run
from benchmark.run import run_suite
from evaluator.duplex import _intersection_ms, evaluate_case, measure_events
from events.replay import file_hash, read_recording
from events.schema import EventDraft, RecordingContext
from scenarios.loader import load_scenario
from scenarios.schema import Scenario
from simulator.audio import wav_bytes


def event(context, kind, ns, payload, **fields):
    return EventDraft(
        **context.model_dump(),
        clock_id="test",
        timestamp_monotonic_ns=ns,
        wall_clock_timestamp="2026-09-20T00:00:00Z",
        event=kind,
        source=fields.pop("source", "user"),
        producer="fixture",
        timing={"basis": "client_receive"},
        payload=payload,
        **fields,
    )


def test_negative_response_gap_and_pause_are_not_filtered():
    context = RecordingContext(run_id="r", scenario_id="s", attempt_id="a", session_id="i")
    rows = [
        event(
            context,
            "user_audio_end",
            900_000_000,
            {"action_id": "a", "end_sample": 900, "annotation_source": "fixture"},
            turn_id="t1",
            stream_id="input",
        ),
        event(
            context,
            "assistant_response_start",
            500_000_000,
            {
                "response_status": "in_progress",
                "trigger_turn_id": "t1",
                "association_method": "vendor_ids",
            },
            source="assistant",
            turn_id="t1",
            response_id="r1",
        ),
    ]
    for kind, ns, sample in (
        ("input_region_start", 400_000_000, 400),
        ("input_region_end", 800_000_000, 800),
    ):
        rows.append(
            event(
                context,
                kind,
                ns,
                {"action_id": "a", "region_id": "p1", "kind": "pause", "sample_index": sample},
                turn_id="t1",
                stream_id="input",
            )
        )
    result = measure_events(rows, category="pause")
    assert result["response_gap_ms"] == -400
    assert result["premature_response"] and not result["turn_completion"]
    assert result["pause_windows"][0]["duration_ms"] == 400
    assert result["premature_response_rate"] == 1
    # An unrelated response cannot replace the response associated with t1.
    rows.append(
        event(
            context,
            "assistant_response_start",
            1_000_000_000,
            {
                "response_status": "in_progress",
                "trigger_turn_id": "t2",
                "association_method": "vendor_ids",
            },
            source="assistant",
            turn_id="t2",
            response_id="r2",
        )
    )
    assert measure_events(rows, category="turn_taking")["response_count"] == 1


def test_overlap_uses_input_handoff_end_and_interval_unions():
    context = RecordingContext(run_id="r", scenario_id="s", attempt_id="a", session_id="i")
    ref = AudioRef(
        **AudioFormat(sample_rate_hz=1000).model_dump(),
        path="audio/x",
        byte_offset=0,
        byte_length=20,
        sample_offset=0,
        sample_count=10,
    )
    rows = [
        event(
            context,
            "user_audio_chunk",
            10_000_000,
            {
                "audio_ref": ref.model_dump(),
                "chunk_index": 0,
                "planned_send_ns": 10_000_000,
                "send_started_ns": 10_000_000,
                "send_completed_ns": 10_000_000,
                "silence": False,
            },
            turn_id="t1",
            stream_id="input",
        ),
        event(
            context,
            "user_audio_end",
            10_000_000,
            {"action_id": "a", "end_sample": 10, "annotation_source": "fixture"},
            turn_id="t1",
            stream_id="input",
        ),
        event(
            context,
            "assistant_playback_chunk",
            5_000_000,
            {
                "sample_offset": 0,
                "sample_count": 10,
                "sample_rate_hz": 1000,
                "audio_ref": ref.model_dump(),
            },
            source="assistant",
            turn_id="t1",
            response_id="r1",
            stream_id="r1",
        ),
    ]
    for kind, ns in (("input_region_start", 0), ("input_region_end", 10_000_000)):
        rows.append(
            event(
                context,
                kind,
                ns,
                {
                    "action_id": "a",
                    "region_id": "bg",
                    "kind": "interferer",
                    "sample_index": ns // 1_000_000,
                },
                turn_id="t1",
                stream_id="input",
            )
        )
    result = measure_events(rows, category="overlap")
    assert result["overlap_duration_ms"] == 5
    assert result["interference_playback_overlap_ms"] == 5
    assert _intersection_ms([(0, 10_000_000), (0, 10_000_000)], [(0, 5_000_000)]) == 5


class DuplexAdapter(ScriptedAdapter):
    def __init__(self, context, sink, clock, mode):
        super().__init__(context, clock)
        self.sink, self.mode, self.task = sink, mode, None
        self.last_end = 0

    def emit(self, kind, payload, **fields):
        e = EventDraft(
            **self.context.model_dump(),
            **self.clock.now().model_dump(),
            event=kind,
            source="system",
            producer="fixture",
            timing={"basis": "client_receive"},
            payload=payload,
            **fields,
        )
        self.queue.put_nowait(e)
        return e

    async def _connect(self):
        self.emit(
            "session_start",
            {"vendor_session_id": None, "adapter_version": "fixture", "capabilities": {}},
        )
        return await super()._connect()

    async def respond(self, turn):
        await asyncio.sleep(0.003)
        if self.mode == "timeout":
            return
        if self.mode == "disconnect":
            self.queue.put_nowait(None)
            return
        rid = "r1"
        fields = {"response_id": rid, "turn_id": turn}
        self.emit(
            "assistant_response_start",
            {
                "response_status": "in_progress",
                "trigger_turn_id": turn,
                "association_method": "vendor_ids",
            },
            **fields,
        )
        ref = await self.sink.store_audio(rid, b"\1\0" * 480, self.config.output_audio)
        chunk = self.emit(
            "assistant_audio_chunk",
            {"audio_ref": ref.model_dump(), "chunk_index": 0},
            stream_id=rid,
            **fields,
        )
        self.emit(
            "assistant_audio_start",
            {
                "first_chunk_event_id": chunk.event_id,
                "audio_format": self.config.output_audio.model_dump(),
            },
            **fields,
        )
        self.emit(
            "assistant_audio_end",
            {
                "reason": "completed",
                "last_chunk_event_id": chunk.event_id,
                "complete": True,
                "completion_source": "fixture",
            },
            **fields,
        )
        self.emit(
            "assistant_response_end",
            {"status": "completed", "completion_source": "fixture"},
            **fields,
        )

    async def _send_audio(self, frame):
        end = frame.sample_offset + len(frame.pcm) // 2
        if self.task is None and (end >= self.last_end or self.mode == "premature"):
            self.task = asyncio.create_task(self.respond(frame.turn_id))
        return await super()._send_audio(frame)

    async def _close(self):
        if self.task:
            await self.task
        self.emit("session_end", {"reason": "fixture_close", "complete": True})
        await super()._close()


@pytest.mark.parametrize(
    "category,mode,status",
    [
        ("pause", "normal", "pass"),
        ("pause", "premature", "fail"),
        ("turn_taking", "normal", "pass"),
        ("turn_taking", "premature", "fail"),
        ("turn_taking", "timeout", "fail"),
        ("overlap", "normal", "unknown"),
        ("pause", "disconnect", "invalid"),
    ],
)
def test_duplex_suite_records_regions_and_re_evaluates(
    tmp_path, scenario_data, category, mode, status
):
    data = deepcopy(scenario_data)
    data.update(category=category, scenario_id=f"{category}_{mode}")
    data["session"]["turn_mode"] = "server_vad"
    data["oracle"]["stimulus"] = category
    data["termination"] = {
        "max_case_duration_ms": 2000,
        "response_timeout_ms": 100,
        "post_stimulus_observation_ms": 100,
        "drain_timeout_ms": 100,
    }
    asset = data["audio"]["assets"]["question"]
    pcm = b"\1\0" * 320 + b"\0\0" * 640 + b"\2\0" * 320
    wav = wav_bytes(pcm, AudioFormat(sample_rate_hz=16000))
    (tmp_path / "input.wav").write_bytes(wav)
    asset.update(
        sha256=hashlib.sha256(wav).hexdigest(),
        speech_bounds_samples=[0, 1280],
        boundary_annotation={"method": "fixture", "status": "synthetic"},
    )
    if category == "pause":
        asset["regions"] = [{"region_id": "p1", "kind": "pause", "bounds_samples": [320, 960]}]
    if category == "overlap":
        asset["regions"] = [
            {
                "region_id": "bg",
                "kind": "interferer",
                "subtype": "ambient_speech",
                "bounds_samples": [320, 960],
            }
        ]
    scenario = Scenario.model_validate(data)
    config = SessionConfig(
        model="fixture",
        input_audio=AudioFormat(sample_rate_hz=16000),
        output_audio=AudioFormat(sample_rate_hz=24000),
    )

    class Registration:
        alias = "fixture"

        def factory(self, config):
            def create(context, sink, clock):
                adapter = DuplexAdapter(context, sink, clock, mode)
                adapter.last_end = 1280
                return adapter

            return create

    root = tmp_path / "run"
    result = asyncio.run(
        run_suite(
            output=root,
            scenarios=(scenario,),
            source_root=tmp_path,
            registration=Registration(),
            model_config=config,
            profile=LatencyProfile(
                tail_silence_ms=0,
                max_send_lateness_ms=100,
                max_send_duration_ms=100,
                max_playback_lateness_ms=100,
            ),
            warmups=0,
        )
    )
    row = result["realtime"]["cases"][0]
    assert row["status"] == status, row
    case = root / "cases" / scenario.scenario_id / "attempt_001"
    recording = read_recording(case, allow_partial=mode == "disconnect")
    assert len([e for e in recording.events if e.event == "user_audio_end"]) == 1
    if category in {"pause", "overlap"}:
        assert len([e for e in recording.events if e.event == "input_region_start"]) == 1
        assert len([e for e in recording.events if e.event == "input_region_end"]) == 1
    if mode == "premature":
        assert row["measurement"]["response_gap_ms"] < 0
    if mode == "timeout":
        assert row["censored"]
        assert result["realtime"]["groups"][0]["ttfa_receive_ms"]["n"] == 0
    before = {p: file_hash(p) for p in case.rglob("*") if p.is_file()}
    assert evaluate_run(root) == result
    assert all(file_hash(p) == h for p, h in before.items())
    assert (root / "report.html").exists()
    if mode == "normal":
        with (case / "events.jsonl").open("ab") as stream:
            stream.write(b"corrupt")
        assert evaluate_case(case)["status"] == "invalid"


def test_all_frozen_duplex_assets_reproduce_without_network():
    from pathlib import Path

    from scripts.prepare_duplex import prepare

    root = Path(__file__).parents[1]
    before = {p: file_hash(p) for p in (root / "datasets/audio/duplex").glob("*.wav")}
    cases = prepare(root)
    assert len(cases) == 9
    for _, path in cases:
        load_scenario(root / "scenarios/realtime" / path, asset_root=root)
    assert before == {p: file_hash(p) for p in before}


@pytest.mark.parametrize("kind", ["outside", "duplicate", "missing"])
def test_pause_schema_rejects_invalid_regions(scenario_data, kind):
    data = deepcopy(scenario_data)
    data["category"] = "pause"
    data["oracle"]["stimulus"] = "pause"
    asset = data["audio"]["assets"]["question"]
    region = {"region_id": "p1", "kind": "pause", "bounds_samples": [320, 960]}
    asset["regions"] = [region]
    if kind == "outside":
        asset["regions"][0]["bounds_samples"] = [0, 2000]
    elif kind == "duplicate":
        asset["regions"].append(region)
    else:
        asset["regions"] = []
    with pytest.raises(ValueError):
        Scenario.model_validate(data)
