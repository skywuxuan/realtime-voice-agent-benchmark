"""Consolidate completed cockpit shard reports into one provider archive."""

from __future__ import annotations

import argparse
import collections
from pathlib import Path

from benchmark.contracts import pretty_json
from events.replay import file_hash
from reports.cockpit_comparison import _read, _report_paths


def _merge_runs(reports: list[dict], used_paths: set[str]) -> list[dict]:
    runs: dict[str, dict] = {}
    for report in reports:
        for run in report.get("runs", []):
            path = run["path"]
            if path not in used_paths:
                continue
            if path in runs and runs[path] != run:
                raise ValueError(f"conflicting run reference: {path}")
            runs[path] = run
    if used_paths != runs.keys():
        raise ValueError("case attempt refers to a run absent from shard metadata")
    return list(runs.values())


def _function_rows(cases: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = collections.defaultdict(list)
    for case in cases:
        grouped[case["function"]].append(case)
    return [
        {
            "name": name,
            "cases": [case["scenario_id"] for case in rows],
            "pass": sum(case["selected_attempt"]["status"] == "pass" for case in rows),
            "fail": sum(case["selected_attempt"]["status"] == "fail" for case in rows),
            "invalid": sum(
                case["selected_attempt"]["status"] == "invalid" for case in rows
            ),
            "duplicate_for_coverage": sum(case["duplicate_for_coverage"] for case in rows),
        }
        for name, rows in sorted(grouped.items())
    ]


def _counts(cases: list[dict], functions: list[dict]) -> dict:
    selected = [case["selected_attempt"] for case in cases]
    eligible = [attempt for attempt in selected if attempt["eligible"]]
    return {
        "unique_cases": len(cases),
        "unique_source_lines": len({case["source_line_number"] for case in cases}),
        "unique_functions": len(functions),
        "total_attempts": sum(len(case["attempts"]) for case in cases),
        "eligible": len(eligible),
        "pass": sum(attempt["status"] == "pass" for attempt in selected),
        "fail": sum(attempt["status"] == "fail" for attempt in selected),
        "invalid": len(selected) - len(eligible),
        "first_call_tool_correct": sum(
            attempt["first_call_tool_accuracy"] == 1 for attempt in eligible
        ),
        "first_call_arguments_correct": sum(
            attempt["first_call_argument_accuracy"] == 1 for attempt in eligible
        ),
        "duplicate_calls": sum(attempt["duplicate_call_count"] for attempt in eligible),
        "functions_all_pass": sum(row["pass"] == len(row["cases"]) for row in functions),
        "functions_some_pass": sum(0 < row["pass"] < len(row["cases"]) for row in functions),
        "functions_zero_pass": sum(row["pass"] == 0 for row in functions),
    }


def consolidate(
    *,
    provider: str,
    progress_path: Path,
    prefix_reports: tuple[Path, ...] = (),
    max_line: int | None = None,
) -> dict:
    progress = _read(progress_path)
    paths = _report_paths(progress_path, prefix_reports=prefix_reports)
    reports = [_read(path) for path in paths]
    if not reports:
        raise ValueError("provider campaign has no shard reports")

    source_conversion = reports[0]["source_conversion"]
    if any(report["source_conversion"] != source_conversion for report in reports[1:]):
        raise ValueError("provider shards refer to different converted sources")

    cases = sorted(
        (
            case
            for report in reports
            for case in report["cases"]
            if max_line is None or case["source_line_number"] <= max_line
        ),
        key=lambda case: case["source_line_number"],
    )
    if not cases:
        raise ValueError("provider archive has no cases in scope")
    lines = [case["source_line_number"] for case in cases]
    if len(lines) != len(set(lines)):
        raise ValueError("provider shards contain duplicate source lines")
    for case in cases:
        if case["selected_attempt"] not in case["attempts"]:
            raise ValueError("selected attempt is absent from case attempt history")

    functions = _function_rows(cases)
    used_runs = {
        attempt["run"] for case in cases for attempt in case["attempts"]
    }
    campaign = {
        key: value
        for key, value in progress.items()
        if key not in {"shards", "updated_at", "status"}
    }
    return {
        "schema_version": "0.1",
        "kind": "cockpit_provider_archive",
        "provider": provider,
        "model": progress["model"],
        "scope": {
            "source_lines": [min(lines), max(lines)],
            "valid_tool_cases": len(cases),
        },
        "campaign": campaign,
        "source_conversion": source_conversion,
        "shard_fingerprints": [
            {
                "name": path.name,
                "sha256": file_hash(path),
                "case_count": len(report["cases"]),
            }
            for path, report in zip(paths, reports)
        ],
        "runs": _merge_runs(reports, used_runs),
        "counts": _counts(cases, functions),
        "functions": functions,
        "cases": cases,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--progress", type=Path, required=True)
    parser.add_argument("--prefix-report", type=Path, action="append", default=[])
    parser.add_argument("--max-line", type=int)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.max_line is not None and args.max_line < 1:
        parser.error("max-line must be positive")
    archive = consolidate(
        provider=args.provider,
        progress_path=args.progress,
        prefix_reports=tuple(args.prefix_report),
        max_line=args.max_line,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(pretty_json(archive), encoding="utf-8")


if __name__ == "__main__":
    main()
