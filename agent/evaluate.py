"""Offline Agent evaluation with sealed evidence, state replay and versioned outputs."""

import argparse
import json
from pathlib import Path

from benchmark.contracts import canonical_json, content_hash
from evaluator.agent import EVALUATOR_VERSION, evaluate_observations
from events.replay import RecordingError, file_hash, read_recording
from reports.html import render
from tools.catalog import ProtocolToolServer, ToolCatalog
from tools.scenarios import AgentScenario
from tools.server import FailureFixture, MockToolServer


def evaluate(root: Path):
    recording = read_recording(root, allow_partial=True)

    def load(name):
        if name not in recording.manifest["files"]:
            raise RecordingError("unsealed evaluation input")
        return json.loads((root / name).read_text())

    config, trial, scenario_data = load("config.json"), load("trial.json"), load("scenario.json")
    if (
        config.get("mode") != "agent_benchmark"
        or config.get("execution_source") != "adapter_events"
    ):
        raise RecordingError("only observed adapter traces can be scored")
    scenario = AgentScenario.model_validate(scenario_data)
    if content_hash(scenario_data) != config["scenario_sha256"]:
        raise RecordingError("scenario hash mismatch")
    rows, state = load("tool_calls.json"), load("state.json")
    if scenario.tool_backend == "protocol_ack_v1":
        catalog_data = load("tool_catalog.json")
        catalog = ToolCatalog.model_validate(catalog_data)
        if (
            catalog.catalog_id != scenario.tool_catalog.catalog_id
            or file_hash(root / "tool_catalog.json") != scenario.tool_catalog.sha256
        ):
            raise RecordingError("sealed tool catalog differs from scenario reference")
        replay = ProtocolToolServer(catalog, enabled=scenario.tools_enabled)
    else:
        replay = MockToolServer(
            failures=tuple(FailureFixture(**f) for f in scenario.failure_schedule),
            calendar_events=scenario.initial_state.get("calendar_events", []),
            enabled=scenario.tools_enabled,
        )
    calls = {e.call_id: e for e in recording.events if e.event == "tool_call_end"}
    results = {e.call_id: e for e in recording.events if e.event == "tool_result"}
    for row in rows:
        call = calls.get(row["call_id"])
        if call is None or call.payload.name != row["tool"]:
            raise RecordingError("execution without observed model call")
        args = call.payload.arguments if call.payload.valid_json else {"invalid_json": True}
        if args != row["arguments"]:
            raise RecordingError("executed arguments differ from observed model call")
        result = replay.execute(row["tool"], args, call_id=row["call_id"])
        recorded = results.get(row["call_id"])
        if recorded is None or result.model_dump(mode="json") != recorded.payload.model_dump(
            mode="json"
        ):
            raise RecordingError("tool result differs from deterministic replay")
    if (
        replay.state() != state
        or replay.trace() != rows
        or set(results) != {r["call_id"] for r in rows}
    ):
        raise RecordingError("state/trace differs from deterministic replay")
    call_evidence = {}
    for event in recording.events:
        if event.event in {"tool_call_start", "tool_result_sent"}:
            key = "call_started_ns" if event.event == "tool_call_start" else "result_sent_ns"
            call_evidence.setdefault(event.call_id, {})[key] = event.timestamp_monotonic_ns
    result = evaluate_observations(scenario, rows, state, call_evidence=call_evidence)
    eligible = recording.manifest["status"] == "complete" and trial["status"] in {
        "completed",
        "model_failed",
    }
    result.update(
        eligible=eligible,
        status=("pass" if result["task_completion"] else "fail") if eligible else "invalid",
    )
    result["correction_windows"] = []
    for index, trigger in enumerate(scenario.turn_triggers, 2):
        if trigger.type != "after_tool_start":
            continue
        start = next(
            (
                e
                for e in recording.events
                if e.event == "user_audio_start" and e.turn_id == f"t{index}"
            ),
            None,
        )
        action = next(
            (
                e
                for e in recording.events
                if e.event == "scenario_action_start" and e.turn_id == f"t{index}"
            ),
            None,
        )
        target = next(
            (e for e in recording.events if action and e.event_id == action.causal_event_id), None
        )
        pending = bool(
            start
            and target
            and target.event == "tool_execution_start"
            and target.timestamp_monotonic_ns <= start.timestamp_monotonic_ns
            and not any(
                e.event == "tool_execution_end"
                and e.call_id == target.call_id
                and e.timestamp_monotonic_ns <= start.timestamp_monotonic_ns
                for e in recording.events
            )
        )
        result["correction_windows"].append(
            {
                "turn_id": f"t{index}",
                "tool_pending_at_speech_start": pending,
                "tool_execution_start": target.event_id if target else None,
                "user_audio_start": start.event_id if start else None,
            }
        )
        if not pending:
            result.update(
                eligible=False,
                task_completion=False,
                status="invalid",
                correction_handling_accuracy=None,
            )
    if trial["status"] != "completed":
        result.update(task_completion=False, status="fail" if result["eligible"] else "invalid")
    result["reasons"] = []
    if trial["status"] != "completed":
        result["reasons"].append(trial["reason"])
    if result["eligible"] and not result["task_completion"]:
        if result["tool_selection_accuracy"] == 0:
            result["reasons"].append("required_tool_sequence_not_completed")
        if result["argument_accuracy"] is not None and result["argument_accuracy"] < 1:
            result["reasons"].append("tool_arguments_mismatch_or_missing")
        if result["dependency_accuracy"] is not None and result["dependency_accuracy"] < 1:
            result["reasons"].append("tool_result_dependency_not_satisfied")
        if result["redundant_identical_call"]:
            result["reasons"].append("redundant_identical_tool_call")
        if result["state_matches"] is False:
            result["reasons"].append("final_state_mismatch")
    result["cleanup_warnings"] = trial.get("cleanup_warnings", [])
    result["execution_status"] = trial["status"]
    result["execution_reason"] = trial["reason"]
    result["attempt_id"] = recording.manifest["context"]["attempt_id"]
    result["input_chunk_ms"] = config.get("input_chunk_ms", scenario.input_chunk_ms)
    result["argument_comparison"] = scenario.argument_comparison
    result["warmup"] = config.get("warmup", False)
    result["run_id"] = recording.manifest["context"]["run_id"]
    sources = (
        "agent/evaluate.py",
        "evaluator/agent.py",
        "tools/server.py",
        "tools/definitions.py",
        "tools/scenarios.py",
        "tools/catalog.py",
        "events/replay.py",
        "events/schema.py",
        "benchmark/contracts.py",
    )
    identity = {
        "version": EVALUATOR_VERSION,
        "manifest": file_hash(root / "manifest.json"),
        "code": {s: file_hash(Path(__file__).parents[1] / s) for s in sources},
    }
    result["evaluation_id"] = "eval_" + content_hash(identity)[:20]
    out = root / "evaluations" / result["evaluation_id"]
    out.mkdir(parents=True, exist_ok=True)
    for name, data in (("config.json", identity), ("metrics.json", result)):
        text = canonical_json(data) + "\n"
        path = out / name
        if path.exists() and path.read_text() != text:
            raise RecordingError("same evaluation identity changed result")
        path.write_text(text)
    (root / "metrics.json").write_text(canonical_json(result) + "\n")
    (root / "report.html").write_text(render(result, title="Voice Agent Benchmark"))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads((args.run / "manifest.json").read_text())
    result = evaluate_suite(args.run) if manifest.get("kind") == "agent_run" else evaluate(args.run)
    print(result["evaluation_id"])


def evaluate_suite(root: Path):
    from evaluator.agent import aggregate
    from events.replay import artifact_path

    manifest = json.loads((root / "manifest.json").read_text())
    if manifest.get("kind") != "agent_run" or manifest.get("status") != "complete":
        raise RecordingError("completed agent run index required")
    cases = []
    for item in manifest["attempts"]:
        case_root = artifact_path(root, item["path"])
        if file_hash(case_root / "manifest.json") != item["manifest_sha256"]:
            raise RecordingError("case manifest differs from sealed agent run")
        try:
            row = evaluate(case_root)
        except (RecordingError, ValueError, OSError):
            row = {
                "scenario_id": item["scenario_id"],
                "attempt_id": item["attempt_id"],
                "status": "invalid",
                "eligible": False,
                "task_completion": False,
                "warmup": item["warmup"],
                "reason": "invalid_agent_artifacts",
            }
        if row["warmup"] != item["warmup"]:
            row.update(eligible=False, status="invalid", warmup=item["warmup"])
        row["artifact_path"] = item["path"]
        cases.append(row)
    identity = {
        "version": EVALUATOR_VERSION,
        "manifest_sha256": file_hash(root / "manifest.json"),
        "case_evaluations": [row.get("evaluation_id") for row in cases],
        "source_sha256": file_hash(Path(__file__)),
    }
    evaluation_id = "eval_" + content_hash(identity)[:20]
    result = {
        "run_id": manifest["run_id"],
        "evaluation_id": evaluation_id,
        "schema_version": "0.1",
        "agent": aggregate(cases),
        "realtime": {"status": "not_run"},
        "response_quality": {"status": "not_run"},
    }
    directory = root / "evaluations" / evaluation_id
    directory.mkdir(parents=True, exist_ok=True)
    for name, data in (("config.json", identity), ("metrics.json", result)):
        payload = canonical_json(data) + "\n"
        path = directory / name
        if path.exists() and path.read_text() != payload:
            raise RecordingError("evaluation identity collision")
        path.write_text(payload)
    (root / "metrics.json").write_text(canonical_json(result) + "\n")
    (root / "report.html").write_text(render(result, title="Voice Agent Benchmark"))
    return result


if __name__ == "__main__":
    main()
