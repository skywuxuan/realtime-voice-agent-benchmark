import json
import os
from pathlib import Path

from agent.artifacts import (
    ARCHIVE_NAME,
    compact_case,
    compact_run,
    restore_run,
    verify_compact_run,
)
from benchmark.contracts import pretty_json
from events.replay import file_hash
from reports.cockpit_batch import _read_run
from reports.cockpit_comparison import _attempt_detail


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(pretty_json(value), encoding="utf-8")


def _fixture_run(root: Path) -> tuple[Path, dict[str, str]]:
    case_path = "cases/case_001/attempt_001"
    case = root / case_path
    files = {
        "config.json": {"profile": {"max_send_lateness_ms": 20}},
        "scenario.json": {
            "scenario_id": "case_001",
            "world": {"source_line_number": 7},
            "user_turns": ["打开空调"],
            "expected_calls": [
                {"tool": "setAirConditionMode", "arguments": {"operation": "OPEN"}}
            ],
            "audio_assets": {
                "t1": {
                    "path": "datasets/rendered/profile/audio.wav",
                    "sha256": "a" * 64,
                    "reference_text": "打开空调",
                }
            },
        },
        "session_config.json": {"requested": {"instructions": "fixture prompt"}},
        "transcript.json": [
            {"event": "user_text_done", "payload": {"text": "打开空调"}},
            {"event": "assistant_text_done", "payload": {"text": "已打开"}},
        ],
        "tool_calls.json": [
            {
                "tool": "setAirConditionMode",
                "arguments": {"operation": "OPEN"},
                "status": "success",
                "error": None,
            }
        ],
    }
    for name, value in files.items():
        _write(case / name, value)
    events = [
        {
            "event": "user_audio_end",
            "clock_id": "clock",
            "timestamp_monotonic_ns": 1_000_000_000,
        },
        {
            "event": "tool_call_end",
            "clock_id": "clock",
            "timestamp_monotonic_ns": 1_900_000_000,
        },
        {
            "event": "assistant_audio_start",
            "clock_id": "clock",
            "timestamp_monotonic_ns": 2_400_000_000,
        },
    ]
    (case / "events.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in events), encoding="utf-8"
    )
    (case / "a.bin").write_bytes(b"same evidence")
    (case / "b.bin").write_bytes(b"same evidence")
    sealed_files = {}
    for path in sorted(item for item in case.iterdir() if item.name != "manifest.json"):
        sealed_files[path.name] = {"sha256": file_hash(path), "bytes": path.stat().st_size}
    _write(case / "manifest.json", {"status": "complete", "files": sealed_files})
    original = {
        path.relative_to(root).as_posix(): file_hash(path)
        for path in sorted(item for item in case.rglob("*") if item.is_file())
    }

    attempt = {
        "scenario_id": "case_001",
        "attempt_id": "attempt_001",
        "warmup": False,
        "path": case_path,
        "status": "completed",
        "manifest_sha256": file_hash(case / "manifest.json"),
    }
    _write(
        root / "manifest.json",
        {
            "kind": "agent_run",
            "status": "complete",
            "run_id": "run_fixture",
            "attempts": [attempt],
        },
    )
    evaluation = {
        "scenario_id": "case_001",
        "attempt_id": "attempt_001",
        "artifact_path": case_path,
        "warmup": False,
        "eligible": True,
        "status": "pass",
        "task_completion": True,
        "records": [],
    }
    metrics = {
        "evaluation_id": "eval_fixture",
        "agent": {"counts": {"pass": 1}, "cases": [evaluation]},
    }
    _write(root / "metrics.json", metrics)
    _write(root / "evaluations/eval_fixture/metrics.json", metrics)
    _write(
        root / "evaluations/eval_fixture/config.json",
        {"manifest_sha256": file_hash(root / "manifest.json")},
    )
    (root / "report.html").write_text("regenerable", encoding="utf-8")
    tts = root.parent / "datasets/rendered/profile/audio.wav"
    tts.parent.mkdir(parents=True)
    tts.write_bytes(b"frozen tts")
    return tts, original


def test_compact_run_keeps_readable_results_and_restores_exact_evidence(tmp_path):
    root = tmp_path / "run"
    tts, original = _fixture_run(root)

    compact = compact_run(root)

    assert not (root / "cases").exists()
    assert not (root / "report.html").exists()
    assert (root / ARCHIVE_NAME).exists()
    assert tts.read_bytes() == b"frozen tts"
    assert verify_compact_run(root, deep=True) == compact
    manifest, metrics = _read_run(root)
    assert manifest["run_id"] == "run_fixture"
    assert metrics["agent"]["counts"] == {"pass": 1}
    row = compact_case(root, "cases/case_001/attempt_001")
    assert row["timing"]["speech_end_to_final_tool_call_ms"] == 900
    assert row["timing"]["speech_end_to_first_tts_frame_ms"] == 1400
    assert row["input_audio_assets"]["t1"]["path"].startswith("datasets/rendered/")
    results = json.loads((root / "results.json").read_text())
    assert results["latency_summary"]["speech_end_to_final_tool_call"]["p50_ms"] == 900
    assert results["latency_summary"]["speech_end_to_first_tts_frame"]["p95_ms"] == 1400
    detail = _attempt_detail(
        {
            "source_line_number": 7,
            "attempts": [row["evaluation"]],
            "selected_attempt": {
                **row["evaluation"],
                "run": str(root),
                "reason": "response_completed",
                "first_call_tool_accuracy": 1.0,
                "first_call_argument_accuracy": 1.0,
                "duplicate_call_count": 0,
                "evaluation_id": "eval_fixture",
            },
        },
        {"name": "setAirConditionMode", "param": {"operation": "OPEN"}},
    )
    assert detail["calls"][0]["tool"] == "setAirConditionMode"
    assert detail["artifact_path"].startswith(str(root / ARCHIVE_NAME) + "#")

    restore_run(root)

    restored = {
        path.relative_to(root).as_posix(): file_hash(path)
        for path in sorted(item for item in (root / "cases").rglob("*") if item.is_file())
    }
    assert restored == original
    assert os.stat(root / "cases/case_001/attempt_001/a.bin").st_ino == os.stat(
        root / "cases/case_001/attempt_001/b.bin"
    ).st_ino


def test_compaction_can_keep_loose_tree(tmp_path):
    root = tmp_path / "run"
    _fixture_run(root)
    compact_run(root, remove_loose=False)
    assert (root / "cases/case_001/attempt_001/events.jsonl").exists()
    assert verify_compact_run(root, deep=True)["attempt_count"] == 1
