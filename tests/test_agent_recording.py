"""Actual sealed-disk pipeline using fake vendor messages, without oracle execution."""

import asyncio

import pytest
from test_qwen import FAKE_SECRET, factory_for, session_config
from test_qwen_tools import ToolSocket

from agent.evaluate import evaluate
from agent.runtime import run_agent_case
from benchmark.config import LatencyProfile
from events.replay import file_hash, read_recording
from tools.scenarios import AgentScenario


@pytest.mark.parametrize(
    "mode,expected_success", [("tools", True), ("no_calls", False), ("retry", True)]
)
def test_agent_sealed_loop_uses_model_events_not_answers(
    tmp_path, scenario_data, context, monkeypatch, mode, expected_success
):
    monkeypatch.setenv("DASHSCOPE_API_KEY", FAKE_SECRET)
    scenario = AgentScenario(
        scenario_id=context.scenario_id,
        scenario_version=1,
        user_turns=("查询上海和北京天气",),
        tools_enabled=("weather",),
        expected_calls=tuple(
            {"tool": "weather", "arguments": {"city": city, "date": "2026-09-20"}}
            for city in (("上海",) if mode == "retry" else ("上海", "北京"))
        ),
        failure_schedule=({"tool": "weather", "invocation": 1, "kind": "timeout"},)
        if mode == "retry"
        else (),
        expected_final_state={"calendar_events": []},
        audio_assets={"query": scenario_data["audio"]["assets"]["question"]},
        turn_assets=("query",),
    )
    root = tmp_path / "agent"
    trial = asyncio.run(
        run_agent_case(
            factory_for(ToolSocket(mode)),
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
    assert trial["status"] == "completed"
    recording = read_recording(root)
    assert len([e for e in recording.events if e.event == "tool_result"]) == (
        2 if expected_success else 0
    )
    before = {p: file_hash(p) for p in root.rglob("*") if p.is_file()}
    result = evaluate(root)
    assert result["task_completion"] is expected_success
    if mode == "retry":
        assert result["failure_recovery_rate"] == 1
        assert [r["status"] for r in result["records"]] == ["error", "success"]
    assert evaluate(root) == result
    assert all(file_hash(p) == digest for p, digest in before.items())
    assert result["completion_claim_status"] == "unknown"
    assert not any(FAKE_SECRET in p.read_text() for p in root.glob("*.json*"))
