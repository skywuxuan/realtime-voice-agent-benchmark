import json

import pytest
import yaml

from benchmark.contracts import canonical_json
from events.replay import file_hash
from reports.cockpit_batch import summarize, write_readable_views


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(data) + "\n", encoding="utf-8")


def _run(root, cases, *, evaluation_id, max_lateness_ms=20):
    attempts = []
    for case_id, status in cases:
        path = f"cases/{case_id}/attempt_001"
        _write(root / path / "session_config.json", {"requested": {"instructions": "fixture prompt"}})
        _write(
            root / path / "config.json",
            {
                "profile": {
                    "max_send_lateness_ms": max_lateness_ms,
                    "max_playback_lateness_ms": max_lateness_ms,
                }
            },
        )
        _write(
            root / path / "manifest.json",
            {
                "status": status,
                "files": {
                    "config.json": {"sha256": file_hash(root / path / "config.json")},
                    "session_config.json": {
                        "sha256": file_hash(root / path / "session_config.json")
                    }
                },
            },
        )
        attempts.append(
            {
                "scenario_id": case_id,
                "attempt_id": "attempt_001",
                "warmup": False,
                "path": path,
                "manifest_sha256": file_hash(root / path / "manifest.json"),
            }
        )
    _write(
        root / "manifest.json",
        {"kind": "agent_run", "status": "complete", "run_id": root.name, "attempts": attempts},
    )
    metrics = {
        "evaluation_id": evaluation_id,
        "agent": {
            "counts": {"attempted": len(attempts)},
            "cases": [
                {
                    "scenario_id": case_id,
                    "attempt_id": "attempt_001",
                    "artifact_path": f"cases/{case_id}/attempt_001",
                    "warmup": False,
                    "eligible": status != "invalid",
                    "status": status,
                    "task_completion": status == "pass",
                    "execution_reason": "input_deadline_missed_before_send"
                    if status == "invalid"
                    else "response_completed",
                    "first_call_tool_accuracy": 1.0 if status != "invalid" else None,
                    "first_call_argument_accuracy": 1.0 if status == "pass" else 0.0,
                    "duplicate_call_count": 0,
                    "records": [],
                }
                for case_id, status in cases
            ],
        },
    }
    _write(root / "metrics.json", metrics)
    _write(root / "evaluations" / evaluation_id / "metrics.json", metrics)
    _write(
        root / "evaluations" / evaluation_id / "config.json",
        {"manifest_sha256": file_hash(root / "manifest.json")},
    )


def test_cockpit_batch_reconciles_only_invalid_cases_with_sealed_retries(tmp_path):
    conversion = tmp_path / "conversion" / "manifest.json"
    compilation = tmp_path / "compilation" / "manifest.json"
    source = {"protocol": {"sha256": "protocol"}, "testset": {"sha256": "testset"}}
    _write(conversion, {"kind": "converted_cockpit_sources", "source": source,
                        "catalog": {"sha256": "pretty-catalog"},
                        "source_catalog_sha256": "catalog",
                        "counts": {"total": 4}, "files": {}})
    selected = [
        {"line_number": 3 if index == 4 else index,
         "sample_index": (index - 1) % 2 + 1,
         "duplicate_for_coverage": index == 4,
         "function_result": {"name": "function_a" if index <= 2 else "function_b"}}
        for index in range(1, 5)
    ]
    _write(compilation.parent / "source.json", {**source, "selected": selected})
    suite_path = compilation.parent / "suite.yaml"
    suite_path.write_text(yaml.safe_dump({"cases": [f"case_{i}.yaml" for i in range(1, 5)]}))
    _write(
        compilation,
        {
            "kind": "compiled_cockpit_dataset",
            "catalog": {"sha256": "catalog"},
            "exposed_tool_scope": "expected_case",
            "suite": "suite.yaml",
            "compilation_id": "fixture",
            "selection": {"duplicate_for_coverage_count": 1},
            "files": {
                "source.json": file_hash(compilation.parent / "source.json"),
                "suite.yaml": file_hash(suite_path),
            },
        },
    )
    main = tmp_path / "main"
    retry = tmp_path / "retry"
    _run(main, [("case_1", "pass"), ("case_2", "invalid"),
                ("case_3", "fail"), ("case_4", "pass")], evaluation_id="eval_main")
    _run(retry, [("case_2", "pass")], evaluation_id="eval_retry", max_lateness_ms=150)
    result = summarize(conversion_manifest=conversion, compilation_manifest=compilation,
                       runs=(main, retry))
    assert result["counts"] == {
        "unique_cases": 4,
        "unique_source_lines": 3,
        "unique_functions": 2,
        "total_attempts": 5,
        "eligible": 4,
        "pass": 3,
        "fail": 1,
        "invalid": 0,
        "first_call_tool_correct": 4,
        "first_call_arguments_correct": 3,
        "duplicate_calls": 0,
        "functions_with_two_passes": 1,
        "functions_with_one_pass": 1,
        "functions_with_zero_passes": 0,
    }
    assert len(result["cases"][1]["attempts"]) == 2
    assert result["cases"][1]["selected_attempt"]["run"] == str(retry)
    assert result["functions"][1]["duplicate_for_coverage"] == 1
    assert [run["timing_profile"]["max_send_lateness_ms"] for run in result["runs"]] == [
        20,
        150,
    ]
    views = write_readable_views(
        runs=(main, retry), compilation_manifest=compilation,
        output_dir=tmp_path / "readable",
    )
    assert (tmp_path / "readable" / "manifest.json").exists()
    assert json.loads((tmp_path / "readable" / "example-session-prompt.json").read_text()) == {
        "requested": {"instructions": "fixture prompt"}
    }
    assert (tmp_path / "readable" / "run-001-metrics.json").read_text().startswith(
        "{\n  "
    )
    assert write_readable_views(
        runs=(main, retry), compilation_manifest=compilation,
        output_dir=tmp_path / "readable",
    ) == views
    assert (main / "metrics.json").read_text().startswith('{"agent":')
    source_window = json.loads(compilation.read_text())
    source_window["selection"]["mode"] = "source_window"
    _write(compilation, source_window)
    window_result = summarize(
        conversion_manifest=conversion,
        compilation_manifest=compilation,
        runs=(main, retry),
    )
    assert window_result["counts"]["functions_all_pass"] == 1
    assert window_result["counts"]["functions_some_pass"] == 1
    assert "functions_with_two_passes" not in window_result["counts"]
    _run(tmp_path / "bad_retry", [("case_1", "pass")], evaluation_id="eval_bad")
    with pytest.raises(ValueError, match="not for an invalid"):
        summarize(conversion_manifest=conversion, compilation_manifest=compilation,
                  runs=(main, tmp_path / "bad_retry"))
