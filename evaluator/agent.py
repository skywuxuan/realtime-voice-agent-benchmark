"""Deterministic observed-call evaluation with result bindings and temporal evidence."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from benchmark.contracts import content_hash
from tools.definitions import ARGUMENT_MODELS
from tools.scenarios import AgentScenario

EVALUATOR_VERSION = "agent-0.4"


def _time(value, world):
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=ZoneInfo(world.get("timezone", "Asia/Shanghai")))
    return stamp.astimezone(timezone.utc).isoformat()


def _args(tool, arguments, scenario):
    if scenario.argument_comparison == "exact":
        return arguments
    data = ARGUMENT_MODELS[tool].model_validate(arguments).model_dump(exclude_none=True)
    if tool == "calendar":
        for key in ("start", "end"):
            if key in data:
                data[key] = _time(data[key], scenario.world)
    return data


def _state(state, scenario):
    if scenario.argument_comparison == "exact":
        return state
    normalized = {**state, "calendar_events": []}
    for event in state.get("calendar_events", []):
        normalized["calendar_events"].append(
            {
                **event,
                "start": _time(event["start"], scenario.world),
                "end": _time(event["end"], scenario.world),
            }
        )
    normalized["calendar_events"].sort(key=lambda event: event["event_id"])
    return normalized


def _resolve(value, bindings):
    if isinstance(value, dict):
        if set(value) == {"$result"}:
            ref = value["$result"]
            result = bindings[ref["step"]]["result"]
            for key in ref["path"]:
                result = result[key]
            return result
        return {key: _resolve(item, bindings) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve(item, bindings) for item in value]
    return value


def _references(value):
    if isinstance(value, dict):
        if set(value) == {"$result"}:
            return {value["$result"]["step"]}
        return set().union(*(_references(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_references(item) for item in value))
    return set()


def _matches(record, expected, scenario):
    try:
        return record["tool"] == expected["tool"] and _args(
            record["tool"], record["arguments"], scenario
        ) == _args(expected["tool"], expected.get("arguments", {}), scenario)
    except (ValueError, KeyError, TypeError):
        return False


def evaluate_observations(
    scenario: AgentScenario,
    records: list[dict],
    final_state: dict,
    *,
    assistant_claimed_completed: bool | None = None,
    call_evidence=None,
):
    expected = list(scenario.expected_calls)
    successful = [record for record in records if record["status"] == "success"]
    compared = successful if scenario.allow_retries else records
    first_tool = (
        float(bool(records) and records[0]["tool"] == expected[0]["tool"])
        if expected
        else None
    )
    first_arguments = (
        float(bool(records) and _matches(records[0], expected[0], scenario))
        if expected
        else None
    )
    seen_success = set()
    duplicate_count = 0
    for record in successful:
        signature = content_hash({"tool": record["tool"], "arguments": record["arguments"]})
        if signature in seen_success:
            duplicate_count += 1
        seen_success.add(signature)
    selection = bool(expected) and [r["tool"] for r in compared] == [e["tool"] for e in expected]
    matched = []
    bindings = {}
    resolved = []
    dependency_checks = []
    for index, item in enumerate(expected):
        dependencies = set(item.get("depends_on", [])) | _references(item.get("arguments", {}))
        actual = compared[index] if index < len(compared) else None
        try:
            wanted = {**item, "arguments": _resolve(item.get("arguments", {}), bindings)}
        except (KeyError, TypeError, IndexError):
            wanted = None
        ok = (
            actual is not None
            and wanted is not None
            and _matches(actual, wanted, scenario)
            and actual["status"] == "success"
        )
        checks = []
        for dependency in sorted(dependencies):
            source = bindings.get(dependency)
            sent = (
                (call_evidence or {}).get(source["call_id"], {}).get("result_sent_ns")
                if source
                else None
            )
            started = (
                (call_evidence or {}).get(actual["call_id"], {}).get("call_started_ns")
                if actual
                else None
            )
            checks.append(sent is not None and started is not None and sent <= started)
        dependency_checks.extend(checks)
        matched.append(bool(ok))
        resolved.append(
            {
                "step_id": item.get("step_id", f"step_{index}"),
                "arguments": wanted["arguments"] if wanted else None,
                "call_id": actual["call_id"] if actual else None,
                "arguments_match": bool(ok),
                "dependencies": sorted(dependencies),
                "dependencies_satisfied": all(checks) if checks else None,
            }
        )
        if ok:
            bindings[item.get("step_id", f"step_{index}")] = actual
    arguments = sum(matched) / max(len(compared), len(expected)) if expected or compared else None
    sequence = (
        bool(expected)
        and len(compared) == len(expected)
        and all(matched)
        and all(dependency_checks)
    )
    forbidden = any(
        _matches(record, item, scenario) for record in records for item in scenario.forbidden_calls
    )
    state_matches = (
        _state(final_state, scenario) == _state(scenario.expected_final_state, scenario)
        if scenario.expected_final_state
        else None
    )
    completion = sequence and not forbidden and state_matches is not False
    errors = [
        (index, record) for index, record in enumerate(records) if record["status"] == "error"
    ]
    recovered = sum(
        any(
            next_record["status"] == "success"
            and next_record["tool"] == record["tool"]
            and next_record["arguments"] == record["arguments"]
            for next_record in records[index + 1 :]
        )
        for index, record in errors
    )
    return {
        "schema_version": "0.1",
        "evaluator_version": EVALUATOR_VERSION,
        "scenario_id": scenario.scenario_id,
        "scenario_sha256": scenario.sha256,
        "tool_selection_accuracy": float(selection) if expected else None,
        "argument_accuracy": arguments,
        "first_call_tool_accuracy": first_tool,
        "first_call_argument_accuracy": first_arguments,
        "redundant_identical_call": bool(duplicate_count),
        "duplicate_call_count": duplicate_count,
        "tool_call_sequence_accuracy": float(sequence) if expected else None,
        "dependency_accuracy": sum(dependency_checks) / len(dependency_checks)
        if dependency_checks
        else None,
        "resolved_steps": resolved,
        "task_completion": completion,
        "state_matches": state_matches,
        "correction_handling_accuracy": float(completion)
        if "correction" in scenario.tags
        else None,
        "failure_recovery_rate": recovered / len(errors) if errors else None,
        "hallucinated_action": assistant_claimed_completed and not completion
        if assistant_claimed_completed is not None
        else None,
        "completion_claim_status": "observed"
        if assistant_claimed_completed is not None
        else "unknown",
        "forbidden_action": forbidden,
        "final_state": final_state,
        "final_state_hash": content_hash(final_state),
        "records": records,
    }


def evaluate_trace(
    scenario: AgentScenario, server, *, assistant_claimed_completed=None
):
    return evaluate_observations(
        scenario,
        server.trace(),
        server.state(),
        assistant_claimed_completed=assistant_claimed_completed,
    )


def aggregate(results):
    scored = [r for r in results if not r.get("warmup", False)]
    eligible = [r for r in scored if r.get("eligible", True)]
    metrics = {}
    for name in (
        "tool_selection_accuracy",
        "argument_accuracy",
        "first_call_tool_accuracy",
        "first_call_argument_accuracy",
        "redundant_identical_call",
        "tool_call_sequence_accuracy",
        "dependency_accuracy",
        "correction_handling_accuracy",
        "failure_recovery_rate",
        "hallucinated_action",
    ):
        known = [result[name] for result in eligible if result.get(name) is not None]
        metrics[name] = sum(known) / len(known) if known else None
        metrics[name + "_known_count"] = len(known)
    return {
        "schema_version": "0.1",
        "evaluator_version": EVALUATOR_VERSION,
        "counts": {
            "attempted": len(scored),
            "eligible": len(eligible),
            "invalid": len(scored) - len(eligible),
            "warmup_count": len(results) - len(scored),
            "task_completion": sum(result["task_completion"] for result in eligible),
            "duplicate_calls": sum(result.get("duplicate_call_count", 0) for result in eligible),
        },
        "task_completion_rate": sum(result["task_completion"] for result in eligible)
        / len(eligible)
        if eligible
        else None,
        "metrics": metrics,
        "cases": results,
    }
