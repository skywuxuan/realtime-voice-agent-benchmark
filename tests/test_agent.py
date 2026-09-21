from evaluator.agent import aggregate, evaluate_trace
from tools.scenarios import AgentScenario
from tools.server import FailureFixture, MockToolServer


def test_deterministic_tool_state_and_task_completion():
    scenario = AgentScenario(
        scenario_id="train_001",
        scenario_version=1,
        user_turns=("查上海到北京的车",),
        tools_enabled=("train",),
        expected_calls=(
            {
                "tool": "train",
                "arguments": {"from_city": "上海", "to_city": "北京", "date": "2026-09-20"},
            },
        ),
        expected_final_state={"calendar_events": []},
    )
    server = MockToolServer()
    server.execute(
        "train", {"from_city": "上海", "to_city": "北京", "date": "2026-09-20"}, call_id="call_001"
    )
    result = evaluate_trace(scenario, server)
    assert result["task_completion"] and result["argument_accuracy"] == 1.0
    assert result["final_state_hash"] == server.state_hash()
    assert aggregate([result])["metrics"]["tool_selection_accuracy"] == 1.0


def test_tool_failure_and_hallucinated_completion_are_separate():
    scenario = AgentScenario(
        scenario_id="calendar_001",
        scenario_version=1,
        user_turns=("建日历",),
        tools_enabled=("calendar",),
        expected_calls=(
            {
                "tool": "calendar",
                "arguments": {
                    "operation": "create",
                    "title": "会议",
                    "start": "2026-09-20T10:00",
                    "end": "2026-09-20T11:00",
                },
            },
        ),
        expected_final_state={
            "calendar_events": [
                {
                    "event_id": "event_001",
                    "title": "会议",
                    "start": "2026-09-20T10:00",
                    "end": "2026-09-20T11:00",
                }
            ]
        },
        failure_schedule=({"tool": "calendar", "invocation": 1, "kind": "http_500"},),
    )
    server = MockToolServer(failures=(FailureFixture("calendar", 1, "http_500"),))
    server.execute(
        "calendar",
        {
            "operation": "create",
            "title": "会议",
            "start": "2026-09-20T10:00",
            "end": "2026-09-20T11:00",
        },
        call_id="call_001",
    )
    result = evaluate_trace(scenario, server, assistant_claimed_completed=True)
    assert not result["task_completion"] and result["hallucinated_action"]
    assert result["failure_recovery_rate"] == 0.0


def test_tool_runtime_emits_auditable_events():
    from events.clock import SystemClock
    from events.schema import RecordingContext
    from tools.runtime import ToolRuntime

    events = []
    server = MockToolServer()
    runtime = ToolRuntime(
        server,
        RecordingContext(run_id="r", scenario_id="s", attempt_id="a", session_id="i"),
        SystemClock(),
        events.append,
    )
    result = runtime.execute(
        "weather",
        {"city": "上海", "date": "2026-09-20"},
        call_id="call_001",
        response_id="response_001",
    )
    assert result.status == "success"
    assert [event.event for event in events] == [
        "tool_execution_start",
        "tool_execution_end",
        "tool_result",
    ]
    assert events[-1].payload.state_hash == server.state_hash()


def test_unchanged_state_and_no_calls_is_not_task_success():
    scenario = AgentScenario(
        scenario_id="read",
        scenario_version=1,
        user_turns=("查天气",),
        tools_enabled=("weather",),
        expected_calls=({"tool": "weather", "arguments": {"city": "上海", "date": "2026-09-20"}},),
        expected_final_state={"calendar_events": []},
    )
    result = evaluate_trace(scenario, MockToolServer())
    assert result["task_completion"] is False
    assert result["argument_accuracy"] == 0
    assert result["hallucinated_action"] is None
    assert result["failure_recovery_rate"] is None


def test_first_call_accuracy_does_not_hide_redundant_execution():
    scenario = AgentScenario(
        scenario_id="duplicate",
        scenario_version=1,
        user_turns=("查天气",),
        tools_enabled=("weather",),
        expected_calls=(
            {"tool": "weather", "arguments": {"city": "上海", "date": "2026-09-20"}},
        ),
        allow_retries=False,
    )
    server = MockToolServer()
    arguments = {"city": "上海", "date": "2026-09-20"}
    server.execute("weather", arguments, call_id="c1")
    server.execute("weather", arguments, call_id="c2")
    result = evaluate_trace(scenario, server)
    assert result["first_call_tool_accuracy"] == 1
    assert result["first_call_argument_accuracy"] == 1
    assert result["redundant_identical_call"] is True
    assert result["duplicate_call_count"] == 1
    assert result["task_completion"] is False


def test_retry_recovery_needs_matching_success_not_any_later_call():
    scenario = AgentScenario(
        scenario_id="retry",
        scenario_version=1,
        user_turns=("查天气",),
        tools_enabled=("weather",),
        expected_calls=({"tool": "weather", "arguments": {"city": "上海", "date": "2026-09-20"}},),
    )
    args = {"city": "上海", "date": "2026-09-20"}
    server = MockToolServer(failures=(FailureFixture("weather", 1, "timeout"),))
    server.execute("weather", args, call_id="c1")
    server.execute("calendar", {"operation": "list"}, call_id="c2")
    assert evaluate_trace(scenario, server)["failure_recovery_rate"] == 0
    server.execute("weather", args, call_id="c3")
    assert evaluate_trace(scenario, server)["failure_recovery_rate"] == 1
    # The extra successful call still makes this a different sequence.
    assert evaluate_trace(scenario, server)["task_completion"] is False
    retry_only = MockToolServer(failures=(FailureFixture("weather", 1, "timeout"),))
    retry_only.execute("weather", args, call_id="c1")
    retry_only.execute("weather", args, call_id="c2")
    assert evaluate_trace(scenario, retry_only)["task_completion"] is True


def test_call_id_replay_does_not_create_duplicate_calendar_event():
    import pytest

    server = MockToolServer()
    args = {
        "operation": "create",
        "title": "讨论",
        "start": "2026-09-20T10:00",
        "end": "2026-09-20T11:00",
    }
    first = server.execute("calendar", args, call_id="same")
    assert server.execute("calendar", args, call_id="same") == first
    assert len(server.state()["calendar_events"]) == len(server.records) == 1
    with pytest.raises(ValueError, match="call_id reused"):
        server.execute("calendar", {**args, "title": "改名"}, call_id="same")
    snapshot = server.state()
    snapshot["calendar_events"][0]["title"] = "mutated"
    assert server.state()["calendar_events"][0]["title"] == "讨论"


def test_strict_arguments_disabled_tool_and_no_result_do_not_mutate_state():
    server = MockToolServer(enabled=("calendar",))
    result = server.execute("weather", {"city": "上海", "date": "2026-09-20"}, call_id="c1")
    assert result.error["kind"] == "permission_denied"
    args = {
        "operation": "create",
        "title": "讨论",
        "start": "2026-09-20T11:00",
        "end": "2026-09-20T10:00",
    }
    assert server.execute("calendar", args, call_id="c2").error["kind"] == "invalid_argument"
    assert (
        server.execute("calendar", {"operation": "list", "extra": "secret"}, call_id="c3").status
        == "error"
    )
    no_result = MockToolServer(failures=(FailureFixture("train", 1, "no_result"),))
    response = no_result.execute(
        "train", {"from_city": "上海", "to_city": "天津", "date": "2026-09-20"}, call_id="c4"
    )
    assert response.status == "success" and response.result["found"] is False
    assert server.state() == {"calendar_events": []}


def test_public_config_never_contains_oracle_data():
    from adapters.base import SessionConfig
    from agent.runtime import public_config
    from benchmark.audio import AudioFormat

    scenario = AgentScenario(
        scenario_id="secret",
        scenario_version=1,
        user_turns=("查天气",),
        tools_enabled=("weather",),
        expected_calls=(
            {"tool": "weather", "arguments": {"city": "ORACLE_ONLY", "date": "2026-09-20"}},
        ),
        expected_final_state={"secret": "STATE_ONLY"},
    )
    config = SessionConfig(
        model="fixture",
        input_audio=AudioFormat(sample_rate_hz=16000),
        output_audio=AudioFormat(sample_rate_hz=24000),
    )
    request = public_config(scenario, config).model_dump_json()
    assert "ORACLE_ONLY" not in request and "STATE_ONLY" not in request
    assert '"name":"weather"' in request


def test_typed_datetime_comparison_does_not_change_stored_state():
    from evaluator.agent import evaluate_trace

    scenario = AgentScenario(
        scenario_id="dates",
        scenario_version=1,
        user_turns=("建会",),
        tools_enabled=("calendar",),
        argument_comparison="typed_iso8601",
        world={"timezone": "Asia/Shanghai"},
        expected_calls=(
            {
                "tool": "calendar",
                "arguments": {
                    "operation": "create",
                    "title": "会议",
                    "start": "2026-09-20T10:00:00",
                    "end": "2026-09-20T11:00:00",
                },
            },
        ),
        expected_final_state={
            "calendar_events": [
                {
                    "event_id": "event_001",
                    "title": "会议",
                    "start": "2026-09-20T10:00:00",
                    "end": "2026-09-20T11:00:00",
                }
            ]
        },
    )
    server = MockToolServer()
    server.execute(
        "calendar",
        {
            "operation": "create",
            "event_id": None,
            "title": "会议",
            "start": "2026-09-20T10:00+08:00",
            "end": "2026-09-20T11:00+08:00",
        },
        call_id="c1",
    )
    before = server.state()
    assert evaluate_trace(scenario, server)["task_completion"]
    assert server.state() == before
    assert "+08:00" in server.state()["calendar_events"][0]["start"]
