import json

import pytest

from reports.cockpit_archive import consolidate


def _case(line: int, *, status: str, eligible: bool = True) -> dict:
    attempt = {
        "run": f"runs/run-{line}",
        "evaluation_id": f"eval-{line}",
        "artifact_path": f"cases/case-{line}/attempt_001",
        "eligible": eligible,
        "status": status,
        "reason": None,
        "task_completion": status == "pass",
        "first_call_tool_accuracy": 1 if eligible else None,
        "first_call_argument_accuracy": 1 if status == "pass" else 0,
        "duplicate_call_count": 0,
        "model_call_count": 1,
    }
    return {
        "scenario_id": f"case-{line}",
        "function": "setVolume",
        "source_line_number": line,
        "sample_index": 0,
        "duplicate_for_coverage": False,
        "selected_attempt": attempt,
        "attempts": [attempt],
    }


def _write_report(path, case: dict) -> None:
    path.write_text(
        json.dumps(
            {
                "source_conversion": {"path": "converted/manifest.json", "sha256": "abc"},
                "runs": [
                    {
                        "path": case["selected_attempt"]["run"],
                        "run_id": case["scenario_id"],
                    }
                ],
                "cases": [case],
            }
        ),
        encoding="utf-8",
    )


def test_consolidate_preserves_cases_attempts_and_runs(tmp_path):
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    _write_report(first, _case(2, status="fail"))
    _write_report(second, _case(1, status="pass"))
    progress = tmp_path / "progress.json"
    progress.write_text(
        json.dumps(
            {
                "schema_version": "0.1",
                "kind": "cockpit_campaign_progress",
                "model": "example-model",
                "status": "complete",
                "updated_at": "2026-09-28T00:00:00Z",
                "shards": [{"report": str(first)}, {"report": str(second)}],
            }
        ),
        encoding="utf-8",
    )

    archive = consolidate(provider="example", progress_path=progress, max_line=1)

    assert archive["kind"] == "cockpit_provider_archive"
    assert [case["source_line_number"] for case in archive["cases"]] == [1]
    assert archive["counts"]["unique_cases"] == 1
    assert archive["counts"]["pass"] == 1
    assert archive["counts"]["fail"] == 0
    assert len(archive["runs"]) == 1
    assert len(archive["shard_fingerprints"]) == 2


def test_consolidate_rejects_duplicate_source_lines(tmp_path):
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    _write_report(first, _case(1, status="pass"))
    _write_report(second, _case(1, status="pass"))
    progress = tmp_path / "progress.json"
    progress.write_text(
        json.dumps(
            {
                "model": "example-model",
                "shards": [{"report": str(first)}, {"report": str(second)}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate source lines"):
        consolidate(provider="example", progress_path=progress)
