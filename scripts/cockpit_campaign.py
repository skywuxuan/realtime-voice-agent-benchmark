"""Run resumable source-line shards with shared frozen Qwen TTS audio."""

import argparse
import json
import os
import time
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path

from agent.artifacts import compact_run
from agent.run import load_inputs, run_cli
from benchmark.contracts import pretty_json
from dataset.cockpit import compile_cockpit_dataset
from dataset.compiler import MissingRenderedAudio
from dataset.schema import TTSProfile
from events.replay import file_hash
from renderers.registry import create_renderer
from reports.cockpit_batch import _read_run, summarize
from scenarios.loader import load_yaml


def source_windows(start_line: int, end_line: int, batch_size: int):
    if start_line < 1 or end_line < start_line or batch_size < 1:
        raise ValueError("invalid source-line campaign range")
    start = start_line
    while start <= end_line:
        count = min(batch_size, end_line - start + 1)
        yield start, start + count - 1
        start += count


def pending_windows(*, windows, sealed_shards, conversion_manifest):
    """Resume after an intact prefix without regenerating old-version reports."""
    windows = tuple(windows)
    if len(sealed_shards) > len(windows):
        raise ValueError("progress contains more shards than the requested source range")
    source_sha256 = file_hash(conversion_manifest)
    for (start, end), shard in zip(windows, sealed_shards):
        if (shard["start_line"], shard["end_line"]) != (start, end):
            raise ValueError("sealed shards must form a contiguous source-line prefix")
        if sum(shard[key] for key in ("valid_tool", "invalid_tool", "no_tool")) != end - start + 1:
            raise ValueError("sealed shard source-line counts do not add up")
        report = json.loads(Path(shard["report"]).read_text(encoding="utf-8"))
        if (
            report["source_conversion"]["manifest_sha256"] != source_sha256
            or report["compilation"]["manifest_sha256"]
            != file_hash(Path(shard["compilation_manifest"]))
            or report["counts"] != shard["counts"]
            or [reference["path"] for reference in report["runs"]] != shard["runs"]
        ):
            raise ValueError("sealed shard report differs from campaign progress")
        for reference in report["runs"]:
            root = Path(reference["path"])
            manifest, metrics = _read_run(root)
            if (
                reference["manifest_sha256"] != file_hash(root / "manifest.json")
                or reference["run_id"] != manifest["run_id"]
                or reference["evaluation_id"] != metrics["evaluation_id"]
                or reference["counts"] != metrics["agent"]["counts"]
            ):
                raise ValueError(f"sealed run differs from shard report: {root}")
    return windows[len(sealed_shards) :]


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_progress(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(pretty_json(state), encoding="utf-8")
    temporary.replace(path)


def _complete_run(prefix: Path, expected_ids: tuple[str, ...]) -> Path | None:
    for root in sorted(prefix.parent.glob(prefix.name + "-*")):
        try:
            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            metrics = json.loads((root / "metrics.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            continue
        observed = tuple(entry["scenario_id"] for entry in manifest.get("attempts", ()))
        if (
            manifest.get("status") == "complete"
            and observed == expected_ids
            and len(metrics.get("agent", {}).get("cases", ())) == len(expected_ids)
        ):
            return root
    return None


def _next_run(prefix: Path) -> Path:
    existing = tuple(prefix.parent.glob(prefix.name + "-*"))
    return prefix.parent / f"{prefix.name}-{len(existing) + 1:03d}"


def _invalid_ids(run: Path) -> tuple[str, ...]:
    metrics = json.loads((run / "metrics.json").read_text(encoding="utf-8"))
    return tuple(
        case["scenario_id"] for case in metrics["agent"]["cases"] if not case["eligible"]
    )


def _run_cases(args, suite: Path, output: Path, case_ids: tuple[str, ...]) -> None:
    run_cli(
        Namespace(
            scenario=suite,
            output=output,
            asset_root=args.asset_root,
            case_id=list(case_ids) if case_ids else None,
            limit=None,
            offset=None,
            model=args.adapter,
            config=args.model_config,
            profile=args.latency_profile,
            repetitions=None,
            warmups=0,
            turn_mode=args.turn_mode,
            artifact_mode="full",
        )
    )


def run_window(args, start: int, end: int, profile: TTSProfile) -> dict:
    dataset_id = (
        f"{args.dataset_prefix}_full_{start:06d}_{end:06d}_expected_tool_v1"
    )
    result = None
    attempt = 0
    while result is None:
        attempt += 1
        try:
            result = compile_cockpit_dataset(
                protocol_path=args.protocol,
                testset_path=args.testset,
                start_line=start,
                source_line_count=end - start + 1,
                dataset_id=dataset_id,
                profile=profile,
                renderer=create_renderer(profile),
                asset_root=args.asset_root,
                allow_render=not args.cache_only,
                target_model=args.target_model,
                input_chunk_ms=args.input_chunk_ms,
                input_sample_rate_hz=getattr(args, "input_sample_rate_hz", None),
                expected_tool_only=True,
                secrets=(os.environ["DASHSCOPE_API_KEY"],)
                if not args.cache_only
                else (),
            )
        except MissingRenderedAudio:
            if not args.await_cache:
                raise
            print(
                {"source_window": [start, end], "waiting_for_frozen_audio_s": args.cache_wait_s},
                flush=True,
            )
            time.sleep(args.cache_wait_s)
        except RuntimeError as error:
            if (
                args.cache_only
                or "TTS HTTPS connection failed" not in str(error)
                or attempt == args.tts_attempts
            ):
                raise
            delay = min(attempt * 30, 120)
            print(
                {"source_window": [start, end], "tts_attempt": attempt, "retry_in_s": delay},
                flush=True,
            )
            time.sleep(delay)
    if args.cache_only and result.provider_calls:
        raise ValueError("cache-only campaign unexpectedly invoked an audio provider")
    scenarios, _ = load_inputs(result.suite_path, asset_root=args.asset_root)
    all_ids = tuple(scenario.scenario_id for scenario in scenarios)
    stem = args.run_root / (
        f"{args.artifact_prefix}-cockpit-full-{start:06d}-{end:06d}-{args.run_date}"
    )
    main_prefix = Path(str(stem) + "-main")
    main = _complete_run(main_prefix, all_ids)
    if main is None:
        main = _next_run(main_prefix)
        _run_cases(args, result.suite_path, main, ())
    runs = [main]
    pending = _invalid_ids(main)
    for retry in range(1, args.invalid_retries + 1):
        if not pending:
            break
        retry_prefix = Path(str(stem) + f"-invalid-retry-{retry:02d}")
        retry_run = _complete_run(retry_prefix, pending)
        if retry_run is None:
            retry_run = _next_run(retry_prefix)
            _run_cases(args, result.suite_path, retry_run, pending)
        runs.append(retry_run)
        pending = _invalid_ids(retry_run)
    report = args.report_root / (
        f"{args.report_prefix}-full-{start:06d}-{end:06d}-{args.run_date}.json"
    )
    summary = summarize(
        conversion_manifest=args.conversion_manifest,
        compilation_manifest=result.manifest_path,
        runs=tuple(runs),
    )
    if report.exists():
        if json.loads(report.read_text(encoding="utf-8")) != summary:
            raise ValueError(f"existing shard report differs: {report}")
    else:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(pretty_json(summary), encoding="utf-8")
    if getattr(args, "artifact_mode", "compact") == "compact":
        for run in runs:
            compact_run(run)
    compilation = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    return {
        "start_line": start,
        "end_line": end,
        "source_line_count": end - start + 1,
        "valid_tool": len(scenarios),
        **compilation["selection"]["excluded"],
        "compilation_manifest": str(result.manifest_path),
        "runs": [str(run) for run in runs],
        "report": str(report),
        "counts": summary["counts"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--testset", type=Path, required=True)
    parser.add_argument("--conversion-manifest", type=Path, required=True)
    parser.add_argument("--start-line", type=int, required=True)
    parser.add_argument("--end-line", type=int, required=True)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--invalid-retries", type=int, default=2)
    parser.add_argument("--tts-attempts", type=int, default=6)
    parser.add_argument("--cache-only", action="store_true")
    parser.add_argument("--await-cache", action="store_true")
    parser.add_argument("--cache-wait-s", type=float, default=30)
    parser.add_argument("--run-date", required=True)
    parser.add_argument("--adapter", default="qwen-realtime")
    parser.add_argument("--target-model", default="qwen-audio-3.0-realtime-flash")
    parser.add_argument("--input-chunk-ms", type=int, default=200)
    parser.add_argument("--input-sample-rate-hz", type=int)
    parser.add_argument("--turn-mode", choices=["manual", "server_vad"])
    parser.add_argument("--dataset-prefix", default="cockpit_audio3")
    parser.add_argument("--artifact-prefix", default="qwen-audio3")
    parser.add_argument("--report-prefix", default="cockpit-audio3")
    parser.add_argument("--asset-root", type=Path, default=Path("."))
    parser.add_argument("--run-root", type=Path, default=Path("runs"))
    parser.add_argument("--report-root", type=Path, default=Path("reports/cockpit-full-shards"))
    parser.add_argument(
        "--progress", type=Path, default=Path("reports/cockpit-full-progress.json")
    )
    parser.add_argument("--tts-profile", type=Path, default=Path("configs/tts/qwen-cherry.yaml"))
    parser.add_argument(
        "--model-config",
        type=Path,
        default=Path("configs/qwen-audio-3.0-realtime-flash-agent.yaml"),
    )
    parser.add_argument("--latency-profile", type=Path, default=Path("configs/latency.yaml"))
    parser.add_argument("--artifact-mode", choices=("full", "compact"), default="compact")
    args = parser.parse_args()
    if (
        args.batch_size < 1
        or args.invalid_retries < 0
        or args.tts_attempts < 1
        or args.input_chunk_ms < 1
        or (args.input_sample_rate_hz is not None and args.input_sample_rate_hz < 1)
        or args.cache_wait_s <= 0
    ):
        parser.error("positive batch/TTS attempts and nonnegative invalid retries required")
    if args.await_cache and not args.cache_only:
        parser.error("await-cache requires cache-only")
    if not args.cache_only and not os.environ.get("DASHSCOPE_API_KEY", "").strip():
        parser.error("DASHSCOPE_API_KEY is required")
    profile = TTSProfile.model_validate(load_yaml(args.tts_profile))
    state = {
        "schema_version": "0.1",
        "kind": "cockpit_full_campaign_progress",
        "status": "running",
        "model": args.target_model,
        "adapter": args.adapter,
        "input_chunk_ms": args.input_chunk_ms,
        "input_sample_rate_hz": args.input_sample_rate_hz,
        "turn_mode": args.turn_mode,
        "cache_only": args.cache_only,
        "audio_renderer": {
            "provider": profile.provider,
            "model": profile.model,
            "voice": profile.voice,
            "profile_id": profile.profile_id,
        },
        "naming": {
            "dataset_prefix": args.dataset_prefix,
            "artifact_prefix": args.artifact_prefix,
            "report_prefix": args.report_prefix,
        },
        "source_range": {"start_line": args.start_line, "end_line": args.end_line},
        "batch_size": args.batch_size,
        "updated_at": _timestamp(),
        "shards": [],
    }
    if args.progress.exists():
        previous = json.loads(args.progress.read_text(encoding="utf-8"))
        previous_identity = {
            "source_range": previous.get("source_range"),
            "model": previous.get("model"),
            "adapter": previous.get("adapter", "qwen-realtime"),
            "input_chunk_ms": previous.get("input_chunk_ms", 200),
            "input_sample_rate_hz": previous.get("input_sample_rate_hz"),
            "turn_mode": previous.get("turn_mode"),
            "cache_only": previous.get("cache_only", False),
            "naming": previous.get(
                "naming",
                {
                    "dataset_prefix": "cockpit_audio3",
                    "artifact_prefix": "qwen-audio3",
                    "report_prefix": "cockpit-audio3",
                },
            ),
        }
        current_identity = {key: state[key] for key in previous_identity}
        if previous_identity != current_identity:
            parser.error("existing progress belongs to a different campaign")
        state["shards"] = previous.get("shards", [])
    remaining = pending_windows(
        windows=source_windows(args.start_line, args.end_line, args.batch_size),
        sealed_shards=state["shards"],
        conversion_manifest=args.conversion_manifest,
    )
    completed = {(item["start_line"], item["end_line"]): item for item in state["shards"]}
    _write_progress(args.progress, state)
    try:
        for start, end in remaining:
            completed[start, end] = run_window(args, start, end, profile)
            state["shards"] = [completed[key] for key in sorted(completed)]
            state["updated_at"] = _timestamp()
            _write_progress(args.progress, state)
        state["status"] = "complete"
    except Exception as error:
        state["status"] = "failed"
        state["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        state["updated_at"] = _timestamp()
        _write_progress(args.progress, state)


if __name__ == "__main__":
    main()
