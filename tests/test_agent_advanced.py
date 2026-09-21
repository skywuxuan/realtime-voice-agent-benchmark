import asyncio
import base64
import json

import pytest
from test_qwen import FAKE_SECRET, FakeSocket, factory_for, session_config

from agent.evaluate import evaluate
from agent.runtime import run_agent_case
from benchmark.config import LatencyProfile
from events.replay import read_recording
from tools.scenarios import AgentScenario

TRAIN = {"from_city": "上海", "to_city": "天津", "date": "2026-09-20"}


class AdvancedSocket(FakeSocket):
    """Model policy fixture reads tool outputs; it cannot access the scenario oracle."""

    def __init__(self, mode):
        super().__init__(auto_response=False)
        self.mode, self.n, self.outputs = mode, 0, {}

    def call(self, rid, name, args, call_id):
        self.push(
            {
                "type": "response.function_call_arguments.done",
                "response_id": rid,
                "name": name,
                "arguments": json.dumps(args),
                "call_id": call_id,
            }
        )

    async def send(self, text):
        await super().send(text)
        data = json.loads(text)
        if data["type"] == "conversation.item.create":
            self.outputs[data["item"]["call_id"]] = json.loads(data["item"]["output"])
        if data["type"] != "response.create":
            return
        self.n += 1
        rid = f"r{self.n}"
        self.push({"type": "response.created", "response": {"id": rid, "status": "in_progress"}})
        if self.n == 1:
            self.call(
                rid,
                "train",
                {**TRAIN, "to_city": "北京"} if self.mode == "correction" else TRAIN,
                "c1",
            )
            if self.mode == "guessed":
                self.call(
                    rid,
                    "calendar",
                    {
                        "operation": "create",
                        "title": "G2",
                        "start": "2026-09-20T08:12:00",
                        "end": "2026-09-20T12:04:00",
                    },
                    "c2",
                )
        elif self.n == 2 and self.mode == "correction":
            self.call(rid, "train", TRAIN, "c2")
        elif self.n == 2 and self.mode == "multi_step":
            row = self.outputs["c1"]["result"]["trains"][0]
            self.call(
                rid,
                "calendar",
                {
                    "operation": "create",
                    "title": row["train_no"],
                    "start": row["depart_at"],
                    "end": row["arrive_at"],
                },
                "c2",
            )
        else:
            self.push(
                {
                    "type": "response.audio.delta",
                    "response_id": rid,
                    "delta": base64.b64encode(b"\1\0" * 480).decode(),
                }
            )
            self.push({"type": "response.audio.done", "response_id": rid})
        self.push({"type": "response.done", "response": {"id": rid, "status": "completed"}})


def binding(field):
    return {"$result": {"step": "search", "path": ["trains", 0, field]}}


@pytest.mark.parametrize("mode", ["multi_step", "guessed", "correction"])
def test_model_result_dependencies_and_inflight_correction(
    tmp_path, scenario_data, context, monkeypatch, mode
):
    monkeypatch.setenv("DASHSCOPE_API_KEY", FAKE_SECRET)
    correction = mode == "correction"
    expected = (
        {
            "step_id": "search",
            "tool": "train",
            "arguments": {**TRAIN, "to_city": "北京"} if correction else TRAIN,
        },
        {"step_id": "corrected", "tool": "train", "arguments": TRAIN}
        if correction
        else {
            "step_id": "write",
            "tool": "calendar",
            "depends_on": ["search"],
            "arguments": {
                "operation": "create",
                "title": binding("train_no"),
                "start": binding("depart_at"),
                "end": binding("arrive_at"),
            },
        },
    )
    state = (
        {"calendar_events": []}
        if correction
        else {
            "calendar_events": [
                {
                    "event_id": "event_001",
                    "title": "G2",
                    "start": "2026-09-20T08:12:00",
                    "end": "2026-09-20T12:04:00",
                }
            ]
        }
    )
    scenario = AgentScenario(
        scenario_id=context.scenario_id,
        scenario_version=1,
        user_turns=("initial", "correction") if correction else ("create from train result",),
        tools_enabled=("train", "calendar"),
        audio_assets={"audio": scenario_data["audio"]["assets"]["question"]},
        turn_assets=("audio", "audio") if correction else ("audio",),
        expected_calls=expected,
        expected_final_state=state,
        tags=("correction",) if correction else ("multi_step",),
        turn_triggers=({"type": "after_tool_start", "tool": "train", "delay_ms": 20},)
        if correction
        else (),
        tool_delays=({"tool": "train", "invocation": 1, "delay_ms": 400},) if correction else (),
        argument_comparison="typed_iso8601",
    )
    socket = AdvancedSocket(mode)
    root = tmp_path / "run"
    trial = asyncio.run(
        run_agent_case(
            factory_for(socket),
            scenario=scenario,
            source_wavs={"input.wav": (tmp_path / "input.wav").read_bytes()},
            output=root,
            context=context,
            config=session_config(),
            profile=LatencyProfile(
                max_send_lateness_ms=100, max_send_duration_ms=100, max_playback_lateness_ms=100
            ),
            secrets=(FAKE_SECRET,),
        )
    )
    assert trial["status"] == "completed", trial
    result = evaluate(root)
    assert result["task_completion"] is (mode != "guessed"), result
    if mode == "guessed":
        assert result["dependency_accuracy"] == 0 and result["argument_accuracy"] == 1
    if correction:
        assert result["correction_windows"][0]["tool_pending_at_speech_start"]
        assert socket.n == 3  # No fourth response created for the superseded Beijing query.
        recording = read_recording(root)
        t2 = next(
            e for e in recording.events if e.event == "user_audio_start" and e.turn_id == "t2"
        )
        done = next(
            e for e in recording.events if e.event == "tool_execution_end" and e.call_id == "c1"
        )
        assert t2.timestamp_monotonic_ns < done.timestamp_monotonic_ns
        assert result["records"][-1]["arguments"]["to_city"] == "天津"
    assert evaluate(root) == result


def test_agent_suite_seals_index_and_uses_unified_evaluation(tmp_path, scenario_data, monkeypatch):
    from agent.run import run_suite
    from benchmark.evaluate import evaluate_run

    monkeypatch.setenv("DASHSCOPE_API_KEY", FAKE_SECRET)
    expected = (
        {"step_id": "search", "tool": "train", "arguments": TRAIN},
        {
            "step_id": "write",
            "tool": "calendar",
            "depends_on": ["search"],
            "arguments": {
                "operation": "create",
                "title": binding("train_no"),
                "start": binding("depart_at"),
                "end": binding("arrive_at"),
            },
        },
    )
    scenario = AgentScenario(
        scenario_id="suite_case",
        scenario_version=1,
        user_turns=("synthetic fixture",),
        tools_enabled=("train", "calendar"),
        audio_assets={"audio": scenario_data["audio"]["assets"]["question"]},
        turn_assets=("audio",),
        expected_calls=expected,
        argument_comparison="typed_iso8601",
    )

    class Registration:
        alias = "fixture"

        def factory(self, config):
            return factory_for(AdvancedSocket("multi_step"))

    root = tmp_path / "suite"
    result = asyncio.run(
        run_suite(
            output=root,
            scenarios=(scenario,),
            source_root=tmp_path,
            registration=Registration(),
            model_config=session_config(),
            profile=LatencyProfile(
                max_send_lateness_ms=100, max_send_duration_ms=100, max_playback_lateness_ms=100
            ),
            repetitions=2,
            warmups=1,
            secrets=(FAKE_SECRET,),
        )
    )
    assert result["agent"]["counts"] == {
        "attempted": 2,
        "eligible": 2,
        "invalid": 0,
        "warmup_count": 1,
        "task_completion": 2,
        "duplicate_calls": 0,
    }
    assert evaluate_run(root) == result
    assert (root / "report.html").exists()


def test_dependency_forward_reference_is_rejected_before_running():
    with pytest.raises(ValueError, match="earlier expected steps"):
        AgentScenario(
            scenario_id="invalid_plan",
            scenario_version=1,
            user_turns=("test",),
            tools_enabled=("calendar",),
            expected_calls=(
                {
                    "step_id": "create",
                    "tool": "calendar",
                    "arguments": {
                        "operation": "create",
                        "title": {"$result": {"step": "future", "path": ["name"]}},
                    },
                },
            ),
        )
