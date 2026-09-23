"""Offline reconciliation of a cockpit suite and retries of invalid attempts."""

import argparse
import collections
import json
from pathlib import Path

from benchmark.contracts import pretty_json
from dataset.compiler import _write_immutable
from events.replay import file_hash
from scenarios.loader import load_yaml


def _read_run(root: Path) -> tuple[dict, dict]:
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("kind") != "agent_run" or manifest.get("status") != "complete":
        raise ValueError(f"run is not complete: {root}")
    metrics = json.loads((root / "metrics.json").read_text(encoding="utf-8"))
    evaluation = root / "evaluations" / metrics["evaluation_id"]
    config = json.loads((evaluation / "config.json").read_text(encoding="utf-8"))
    if config["manifest_sha256"] != file_hash(manifest_path):
        raise ValueError(f"run changed since evaluation: {root}")
    if json.loads((evaluation / "metrics.json").read_text(encoding="utf-8")) != metrics:
        raise ValueError(f"run metrics differ from sealed evaluation: {root}")
    entries = manifest["attempts"]
    cases = metrics["agent"]["cases"]
    if len(entries) != len(cases):
        raise ValueError(f"run index differs from evaluation: {root}")
    for entry, case in zip(entries, cases):
        if (
            entry["scenario_id"] != case["scenario_id"]
            or entry["attempt_id"] != case["attempt_id"]
            or entry["warmup"]
            or case["warmup"]
            or entry["manifest_sha256"]
            != file_hash(root / entry["path"] / "manifest.json")
        ):
            raise ValueError(f"case index differs from sealed evaluation: {root}")
    return manifest, metrics


def summarize(
    *,
    conversion_manifest: Path,
    compilation_manifest: Path,
    runs: tuple[Path, ...],
) -> dict:
    if not runs:
        raise ValueError("provide the complete main run followed by invalid-only retries")
    conversion = json.loads(conversion_manifest.read_text(encoding="utf-8"))
    compilation = json.loads(compilation_manifest.read_text(encoding="utf-8"))
    if (
        conversion.get("kind") != "converted_cockpit_sources"
        or compilation.get("kind") != "compiled_cockpit_dataset"
    ):
        raise ValueError("cockpit source conversion and compilation are required")
    for name, digest in conversion["files"].items():
        if file_hash(conversion_manifest.parent / name) != digest:
            raise ValueError("converted source file differs from manifest")
    for name, digest in compilation["files"].items():
        if file_hash(compilation_manifest.parent / name) != digest:
            raise ValueError("compiled source file differs from manifest")
    if (
        conversion["source"] != {
            key: value
            for key, value in json.loads(
                (compilation_manifest.parent / "source.json").read_text(encoding="utf-8")
            ).items()
            if key in {"protocol", "testset"}
        }
        or conversion["source_catalog_sha256"] != compilation["catalog"]["sha256"]
    ):
        raise ValueError("compiled cases refer to different converted sources")
    if compilation["exposed_tool_scope"] != "expected_case":
        raise ValueError("batch results require a single expected tool per case")
    per_function_batch = compilation["selection"].get("mode", "per_function") == "per_function"

    source = json.loads((compilation_manifest.parent / "source.json").read_text())
    suite = load_yaml(compilation_manifest.parent / compilation["suite"])
    if len(source["selected"]) != len(suite["cases"]):
        raise ValueError("source selection and suite case counts differ")
    scenarios = {}
    source_occurrences = collections.defaultdict(set)
    for selection, filename in zip(source["selected"], suite["cases"]):
        case_id = Path(filename).stem
        if case_id in scenarios:
            raise ValueError("duplicate suite case ID")
        function = selection["function_result"]["name"]
        line_number = selection["line_number"]
        seen = line_number in source_occurrences[function]
        if seen != selection["duplicate_for_coverage"]:
            raise ValueError("sparse duplicate label differs from selected source rows")
        source_occurrences[function].add(line_number)
        scenarios[case_id] = selection

    references = []
    attempts: dict[str, list[dict]] = collections.defaultdict(list)
    main_case_ids = set()
    for index, root in enumerate(runs):
        manifest, metrics = _read_run(root)
        timing_profile = None
        for entry in manifest["attempts"]:
            case_root = root / entry["path"]
            sealed = json.loads((case_root / "manifest.json").read_text(encoding="utf-8"))
            config_file = sealed["files"].get("config.json")
            if config_file is None:
                continue
            config_path = case_root / "config.json"
            if file_hash(config_path) != config_file["sha256"]:
                raise ValueError(f"timing config differs from sealed case: {case_root}")
            current = json.loads(config_path.read_text(encoding="utf-8"))["profile"]
            if timing_profile is not None and timing_profile != current:
                raise ValueError(f"run contains mixed timing profiles: {root}")
            timing_profile = current
        reference = {
            "path": str(root),
            "run_id": manifest["run_id"],
            "manifest_sha256": file_hash(root / "manifest.json"),
            "evaluation_id": metrics["evaluation_id"],
            "counts": metrics["agent"]["counts"],
        }
        if timing_profile is not None:
            reference["timing_profile"] = timing_profile
        references.append(reference)
        cases = metrics["agent"]["cases"]
        ids = [case["scenario_id"] for case in cases]
        if len(ids) != len(set(ids)) or set(ids) - scenarios.keys():
            raise ValueError("run contains duplicate or unknown suite cases")
        if index == 0:
            main_case_ids = set(ids)
            if main_case_ids != scenarios.keys():
                raise ValueError("main run must contain the full suite")
        for case in cases:
            case_id = case["scenario_id"]
            if index and (
                case_id not in main_case_ids
                or any(attempt["eligible"] for attempt in attempts[case_id])
            ):
                raise ValueError("retry is not for an invalid main-run case")
            attempts[case_id].append(
                {
                    "run": str(root),
                    "evaluation_id": metrics["evaluation_id"],
                    "artifact_path": case["artifact_path"],
                    "eligible": case["eligible"],
                    "status": case["status"],
                    "reason": case.get("execution_reason"),
                    "task_completion": case["task_completion"],
                    "first_call_tool_accuracy": case.get("first_call_tool_accuracy"),
                    "first_call_argument_accuracy": case.get("first_call_argument_accuracy"),
                    "duplicate_call_count": case.get("duplicate_call_count", 0),
                    "model_call_count": len(case.get("records", [])),
                }
            )

    grouped: dict[str, list[dict]] = collections.defaultdict(list)
    rows = []
    for case_id, selection in scenarios.items():
        observed = attempts[case_id]
        eligible = next((entry for entry in observed if entry["eligible"]), None)
        chosen = eligible or observed[-1]
        row = {
            "scenario_id": case_id,
            "function": selection["function_result"]["name"],
            "source_line_number": selection["line_number"],
            "sample_index": selection["sample_index"],
            "duplicate_for_coverage": selection["duplicate_for_coverage"],
            "selected_attempt": chosen,
            "attempts": observed,
        }
        rows.append(row)
        grouped[row["function"]].append(row)
    if len(rows) != len(scenarios):
        raise ValueError("batch does not cover every compiled scenario")
    if per_function_batch and any(len(items) != 2 for items in grouped.values()):
        raise ValueError("per-function batch is not two cases per function")
    selected = [row["selected_attempt"] for row in rows]
    valid = [row for row in selected if row["eligible"]]
    functions = [
        {
            "name": name,
            "cases": [row["scenario_id"] for row in items],
            "pass": sum(row["selected_attempt"]["status"] == "pass" for row in items),
            "fail": sum(row["selected_attempt"]["status"] == "fail" for row in items),
            "invalid": sum(row["selected_attempt"]["status"] == "invalid" for row in items),
            "duplicate_for_coverage": sum(row["duplicate_for_coverage"] for row in items),
        }
        for name, items in grouped.items()
    ]
    counts = {
        "unique_cases": len(rows),
        "unique_source_lines": len({row["source_line_number"] for row in rows}),
        "unique_functions": len(functions),
        "total_attempts": sum(len(row["attempts"]) for row in rows),
        "eligible": len(valid),
        "pass": sum(row["status"] == "pass" for row in selected),
        "fail": sum(row["status"] == "fail" for row in selected),
        "invalid": len(rows) - len(valid),
        "first_call_tool_correct": sum(row["first_call_tool_accuracy"] == 1 for row in valid),
        "first_call_arguments_correct": sum(
            row["first_call_argument_accuracy"] == 1 for row in valid
        ),
        "duplicate_calls": sum(row["duplicate_call_count"] for row in valid),
        "functions_all_pass": sum(item["pass"] == len(item["cases"]) for item in functions),
        "functions_some_pass": sum(0 < item["pass"] < len(item["cases"]) for item in functions),
        "functions_zero_pass": sum(item["pass"] == 0 for item in functions),
    }
    if per_function_batch:
        counts.update(
            {
                "functions_with_two_passes": counts["functions_all_pass"],
                "functions_with_one_pass": counts["functions_some_pass"],
                "functions_with_zero_passes": counts["functions_zero_pass"],
            }
        )
        for key in ("functions_all_pass", "functions_some_pass", "functions_zero_pass"):
            counts.pop(key)
    return {
        "schema_version": "0.1",
        "kind": "cockpit_per_function_batch_summary",
        "source_conversion": {
            "path": str(conversion_manifest),
            "manifest_sha256": file_hash(conversion_manifest),
            "counts": conversion["counts"],
        },
        "compilation": {
            "path": str(compilation_manifest),
            "manifest_sha256": file_hash(compilation_manifest),
            "compilation_id": compilation["compilation_id"],
            "exposed_tool_scope": compilation["exposed_tool_scope"],
            "duplicate_for_coverage_count": compilation["selection"]["duplicate_for_coverage_count"],
        },
        "interpretation": [
            "Each model session exposes only the expected function schema; tool-name accuracy"
            " does not measure selection among 100 functions.",
            "The sparse function repeat reuses one source utterance in a separate model session;"
            " it is not a second distinct test case.",
            "Only retries of invalid attempts are eligible for replacement; all original and"
            " retry attempts remain listed below.",
        ],
        "runs": references,
        "counts": counts,
        "functions": functions,
        "cases": rows,
    }


def write_readable_views(
    *, runs: tuple[Path, ...], compilation_manifest: Path, output_dir: Path
) -> dict[str, str]:
    if not runs:
        raise ValueError("at least one sealed run is required")
    for root in runs:
        if output_dir.resolve().is_relative_to(root.resolve()):
            raise ValueError("readable views cannot be written inside a sealed run")
    source_files = {
        "selection.json": compilation_manifest.parent / "source.json",
    }
    source_files.update(
        {f"run-{index:03d}-metrics.json": root / "metrics.json" for index, root in enumerate(runs, 1)}
    )
    main_manifest = json.loads((runs[0] / "manifest.json").read_text(encoding="utf-8"))
    first_case = runs[0] / main_manifest["attempts"][0]["path"]
    case_manifest = json.loads((first_case / "manifest.json").read_text(encoding="utf-8"))
    prompt_file = first_case / "session_config.json"
    if case_manifest["files"]["session_config.json"]["sha256"] != file_hash(prompt_file):
        raise ValueError("example session prompt differs from sealed case")
    source_files["example-session-prompt.json"] = prompt_file
    provenance = {}
    for name, source in source_files.items():
        view = output_dir / name
        if view.resolve() == source.resolve():
            raise ValueError("a readable view cannot overwrite its sealed source")
        _write_immutable(
            view,
            pretty_json(json.loads(source.read_text(encoding="utf-8"))).encode("utf-8"),
        )
        provenance[name] = {
            "source": str(source),
            "source_sha256": file_hash(source),
            "view_sha256": file_hash(view),
        }
    _write_immutable(
        output_dir / "manifest.json", pretty_json({"files": provenance}).encode("utf-8")
    )
    return {name: str(output_dir / name) for name in source_files}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--conversion-manifest", type=Path, required=True)
    parser.add_argument("--compilation-manifest", type=Path, required=True)
    parser.add_argument("--run", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--views-dir", type=Path)
    args = parser.parse_args()
    result = summarize(
        conversion_manifest=args.conversion_manifest,
        compilation_manifest=args.compilation_manifest,
        runs=tuple(args.run),
    )
    if args.views_dir:
        result["readable_views"] = write_readable_views(
            runs=tuple(args.run),
            compilation_manifest=args.compilation_manifest,
            output_dir=args.views_dir,
        )
    _write_immutable(args.output, pretty_json(result).encode("utf-8"))
    print(json.dumps({"output": str(args.output), **result["counts"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
