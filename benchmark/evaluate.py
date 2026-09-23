"""Offline evaluation: python -m benchmark.evaluate --run runs/<run_id>."""

import argparse
import json
from pathlib import Path

from benchmark.config import LatencyProfile
from benchmark.contracts import content_hash, pretty_json
from evaluator.backchannel import EVALUATOR_VERSION as BACKCHANNEL_EVALUATOR_VERSION
from evaluator.backchannel import aggregate as aggregate_backchannel
from evaluator.backchannel import evaluate_case as evaluate_backchannel_case
from evaluator.duplex import EVALUATOR_VERSION as DUPLEX_EVALUATOR_VERSION
from evaluator.duplex import aggregate as aggregate_duplex
from evaluator.duplex import evaluate_case as evaluate_duplex_case
from evaluator.interruption import EVALUATOR_VERSION as INTERRUPTION_EVALUATOR_VERSION
from evaluator.interruption import aggregate as aggregate_interruption
from evaluator.interruption import evaluate_case as evaluate_interruption_case
from evaluator.realtime import EVALUATOR_VERSION, aggregate, evaluate_case
from events.replay import artifact_path, file_hash
from scenarios.loader import load_yaml


def evaluate_run(root: Path, *, profile: LatencyProfile | None = None) -> dict:
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("kind") == "agent_run":
        from agent.evaluate import evaluate_suite

        return evaluate_suite(root)
    if manifest.get("kind") != "benchmark_run" or manifest.get("status") != "complete":
        raise ValueError("offline aggregate requires a completed run manifest")
    cases = []
    hashes = []
    modes = set()
    for entry in manifest["attempts"]:
        case_root = artifact_path(root, entry["path"])
        digest = file_hash(case_root / "manifest.json")
        if digest != entry["manifest_sha256"]:
            raise ValueError("case manifest differs from the sealed run index")
        hashes.append(digest)
        case_mode = json.loads((case_root / "config.json").read_text()).get("mode")
        modes.add(case_mode)
        if case_mode == "interruption_benchmark":
            result = evaluate_interruption_case(case_root, profile=profile)
        elif case_mode == "backchannel_benchmark":
            result = evaluate_backchannel_case(case_root, profile=profile)
        elif case_mode == "duplex_benchmark":
            result = evaluate_duplex_case(case_root, profile=profile)
        else:
            result = evaluate_case(case_root, profile=profile)
        if result["warmup"] != entry["warmup"]:
            # A corrupt/missing config must not turn a warmup into a scored case.
            result.update(warmup=entry["warmup"], status="invalid", measurement_valid=False)
            result["reasons"].append("warmup_metadata_mismatch")
        cases.append(result)
        result["artifact_path"] = entry["path"]
    if len(modes) > 1:
        raise ValueError("a run cannot mix latency and interruption cases; evaluate separate runs")
    is_interruption = any(
        json.loads((artifact_path(root, entry["path"]) / "config.json").read_text()).get("mode")
        == "interruption_benchmark"
        for entry in manifest["attempts"]
    )
    is_backchannel = any(
        json.loads((artifact_path(root, entry["path"]) / "config.json").read_text()).get("mode")
        == "backchannel_benchmark"
        for entry in manifest["attempts"]
    )
    is_duplex = any(
        json.loads((artifact_path(root, entry["path"]) / "config.json").read_text()).get("mode")
        == "duplex_benchmark"
        for entry in manifest["attempts"]
    )
    evaluator_path = Path(__file__).parents[1] / (
        "evaluator/interruption.py"
        if is_interruption
        else "evaluator/backchannel.py"
        if is_backchannel
        else "evaluator/duplex.py"
        if is_duplex
        else "evaluator/realtime.py"
    )
    evaluation_config = {
        "evaluator_version": (
            INTERRUPTION_EVALUATOR_VERSION
            if is_interruption
            else BACKCHANNEL_EVALUATOR_VERSION
            if is_backchannel
            else DUPLEX_EVALUATOR_VERSION
            if is_duplex
            else EVALUATOR_VERSION
        ),
        "evaluator_sha256": file_hash(evaluator_path),
        "dependency_hashes": {
            path: file_hash(Path(__file__).parents[1] / path)
            for path in (
                "benchmark/evaluate.py",
                "benchmark/config.py",
                "benchmark/contracts.py",
                "benchmark/audio.py",
                "events/schema.py",
                "events/replay.py",
                "scenarios/schema.py",
                "evaluator/realtime.py",
                "evaluator/interruption.py",
            )
        },
        "run_manifest_sha256": file_hash(manifest_path),
        "case_manifest_hashes": hashes,
        "profile_override": profile.model_dump(mode="json") if profile else None,
    }
    evaluation_id = "eval_" + content_hash(evaluation_config)[:20]
    result = {
        "run_id": manifest["run_id"],
        "evaluation_id": evaluation_id,
        **(
            aggregate_interruption(cases)
            if is_interruption
            else aggregate_backchannel(cases)
            if is_backchannel
            else aggregate_duplex(cases)
            if is_duplex
            else aggregate(cases)
        ),
    }
    directory = root / "evaluations" / evaluation_id
    directory.mkdir(parents=True, exist_ok=True)
    for name, data in (("config.json", evaluation_config), ("metrics.json", result)):
        payload = pretty_json(data)
        path = directory / name
        if path.exists() and path.read_text() != payload:
            raise ValueError("same evaluation identity produced different output")
        path.write_text(payload, encoding="utf-8")
    (root / "metrics.json").write_text(pretty_json(result), encoding="utf-8")
    from reports.html import write as write_html_report

    write_html_report(root / "metrics.json", root / "report.html")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--profile", type=Path)
    args = parser.parse_args()
    profile = LatencyProfile.model_validate(load_yaml(args.profile)) if args.profile else None
    result = evaluate_run(args.run, profile=profile)
    print(
        json.dumps(
            {
                "evaluation_id": result["evaluation_id"],
                **(
                    result.get("realtime", {}).get("counts")
                    or result.get("agent", {}).get("counts", {})
                ),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
