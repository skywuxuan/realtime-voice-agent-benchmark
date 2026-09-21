import json
from pathlib import Path

from reports.html import render, write


def test_report_is_self_contained(tmp_path: Path):
    metrics = {"realtime": {"counts": {"pass": 1}, "cases": []}}
    path = tmp_path / "metrics.json"
    path.write_text(json.dumps(metrics), encoding="utf-8")
    output = write(path, tmp_path / "report.html")
    text = output.read_text(encoding="utf-8")
    assert "pass" in text and "Metrics JSON" in text and "<script src" not in text
    assert render(metrics).startswith("<!doctype html>")


def test_agent_suite_report_shows_metrics_and_escapes_untrusted_text():
    data = {
        "realtime": {"status": "not_run"},
        "agent": {
            "counts": {"attempted": 1, "eligible": 1},
            "task_completion_rate": 1.0,
            "cases": [
                {
                    "scenario_id": "<script>alert(1)</script>",
                    "status": "pass",
                    "eligible": True,
                    "task_completion": True,
                    "artifact_path": "cases/example/attempt_001",
                    "records": [],
                }
            ],
        },
    }
    report = render(data)
    assert "Agent Metrics" in report and "任务完成率" in report
    assert "cases/example/attempt_001/state.json" in report
    assert "<script>alert" not in report and "&lt;script&gt;" in report
