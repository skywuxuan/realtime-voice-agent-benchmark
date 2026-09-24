"""Run the Phase 3 latency suite through a registered model adapter."""

import argparse
import asyncio
import hashlib
import json
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

from adapters.base import SessionConfig
from adapters.registry import resolve_adapter
from benchmark.config import LatencyProfile
from benchmark.contracts import pretty_json
from benchmark.evaluate import evaluate_run
from benchmark.runner import run_case
from events.redaction import Redactor
from events.replay import artifact_path, file_hash
from events.schema import RecordingContext
from scenarios.loader import load_scenario, load_suite, load_yaml


def implementation_hash() -> str:
    root = Path(__file__).parents[1]
    digest = hashlib.sha256()
    for package in (
        "benchmark",
        "adapters",
        "events",
        "simulator",
        "scenarios",
        "evaluator",
        "agent",
        "tools",
        "reports",
        "dataset",
        "renderers",
    ):
        for path in sorted((root / package).rglob("*.py")):
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def write_index(root: Path, manifest: dict) -> None:
    temp = root / "manifest.json.tmp"
    temp.write_text(pretty_json(manifest), encoding="utf-8")
    temp.replace(root / "manifest.json")


async def run_suite(
    *,
    output: Path,
    scenarios: tuple,
    source_root: Path,
    registration,
    model_config: SessionConfig,
    profile: LatencyProfile,
    repetitions: int = 1,
    warmups: int = 1,
    secrets: tuple[str, ...] = (),
    turn_mode: str | None = None,
    source_compilation: dict | None = None,
) -> dict:
    if not scenarios or repetitions < 1 or warmups < 0:
        raise ValueError(
            "suite must contain cases with positive repetitions and nonnegative warmups"
        )
    categories = {scenario.category for scenario in scenarios}
    if len(categories) != 1 or not categories <= {
        "latency",
        "interruption",
        "backchannel",
        "turn_taking",
        "pause",
        "overlap",
    }:
        raise ValueError("run each supported category separately; mixed suites are not supported")
    for scenario in scenarios:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", scenario.scenario_id):
            raise ValueError("scenario_id must be a safe directory component")
    run_id = "run_" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ_") + uuid.uuid4().hex[:8]
    output.mkdir(parents=True, exist_ok=False)
    fingerprint = implementation_hash()
    manifest = {
        "schema_version": "0.1",
        "kind": "benchmark_run",
        "status": "running",
        "run_id": run_id,
        "adapter": registration.alias,
        "implementation_sha256": fingerprint,
        "source_compilation": source_compilation,
        "attempts": [],
    }
    write_index(output, manifest)
    root_config, _ = Redactor(secrets).clean(
        {
            "adapter": registration.alias,
            "model_config": model_config.model_dump(mode="json"),
            "latency_profile": profile.model_dump(mode="json"),
            "repetitions": repetitions,
            "warmups": warmups,
            "turn_mode_override": turn_mode,
            "source_compilation": source_compilation,
        }
    )
    (output / "config.json").write_text(pretty_json(root_config), encoding="utf-8")
    jobs = [(scenarios[0], f"warmup_{index:03}", True) for index in range(1, warmups + 1)]
    jobs.extend(
        (scenario, f"attempt_{repeat:03}", False)
        for repeat in range(1, repetitions + 1)
        for scenario in scenarios
    )
    for scenario, attempt_id, warmup in jobs:
        resolved = model_config.model_dump()
        resolved.update(scenario.model_session_options().model_dump())
        if turn_mode:
            resolved["turn_mode"] = turn_mode
        config = SessionConfig.model_validate(resolved)
        relative = f"cases/{scenario.scenario_id}/{attempt_id}"
        source_wavs = {
            asset.path: artifact_path(source_root, asset.path).read_bytes()
            for asset in scenario.audio.assets.values()
        }
        context = RecordingContext(
            run_id=run_id,
            scenario_id=scenario.scenario_id,
            attempt_id=attempt_id,
            session_id="session_" + uuid.uuid4().hex,
        )
        trial = await run_case(
            registration.factory(config),
            scenario=scenario,
            source_wavs=source_wavs,
            output=output / relative,
            context=context,
            config=config,
            profile=profile,
            secrets=secrets,
            warmup=warmup,
            implementation_hash=fingerprint,
        )
        manifest["attempts"].append(
            {
                "path": relative,
                "scenario_id": scenario.scenario_id,
                "scenario_sha256": scenario.sha256,
                "attempt_id": attempt_id,
                "warmup": warmup,
                "status": trial["status"],
                "manifest_sha256": file_hash(output / relative / "manifest.json"),
            }
        )
        write_index(output, manifest)
        print(
            json.dumps(
                {
                    "scenario_id": scenario.scenario_id,
                    "attempt_id": attempt_id,
                    "warmup": warmup,
                    "status": trial["status"],
                    "reason": trial["reason"],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    manifest["status"] = "complete"
    write_index(output, manifest)
    return evaluate_run(output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="qwen-realtime", help="registered adapter alias")
    parser.add_argument("--suite", choices=["realtime", "agent"], default="realtime")
    parser.add_argument("--scenario", type=Path, required=True)
    parser.add_argument("--asset-root", type=Path, default=Path("."))
    parser.add_argument("--config", type=Path)
    parser.add_argument(
        "--profile", type=Path, default=Path(__file__).parents[1] / "configs/latency.yaml"
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--repetitions", type=int)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--case-id", action="append", help="agent suite case ID to run")
    parser.add_argument("--turn-mode", choices=["manual", "server_vad"])
    parser.add_argument("--artifact-mode", choices=("full", "compact"), default="full")
    parser.add_argument(
        "--render-missing",
        action="store_true",
        help="render missing text-source audio before opening any benchmark session",
    )
    parser.add_argument(
        "--tts-profile",
        type=Path,
        default=Path(__file__).parents[1] / "configs/tts/qwen-cherry.yaml",
    )
    parser.add_argument("--render-root", type=Path, default=Path("datasets/rendered"))
    parser.add_argument("--compiled-root", type=Path, default=Path("scenarios/compiled"))
    args = parser.parse_args()
    if args.warmups < 0 or (args.repetitions is not None and args.repetitions < 1):
        parser.error("warmups must be nonnegative and repetitions must be positive")
    if args.suite == "agent":
        from agent.run import run_cli

        run_cli(args)
        return
    if args.artifact_mode != "full":
        parser.error("compact artifacts are currently supported only for agent suites")
    if args.case_id:
        parser.error("case-id is supported only for agent suites")
    registration = resolve_adapter(args.model)
    secrets = tuple(os.environ.get(name, "").strip() for name in registration.credential_variables)
    if any(not value for value in secrets):
        parser.error("required adapter credential environment variables are missing")
    scenario_path = args.scenario
    source_compilation = None
    from dataset.compiler import is_text_source

    if is_text_source(scenario_path):
        from dataset.render import compile_source_path

        try:
            compilation = compile_source_path(
                scenario_path,
                profile_path=args.tts_profile,
                asset_root=args.asset_root,
                render_root=args.render_root,
                compiled_root=args.compiled_root,
                allow_render=args.render_missing,
            )
        except ValueError as error:
            parser.error(str(error))
        scenario_path = compilation.suite_path
        source_compilation = {
            "compilation_id": compilation.compilation_id,
            "manifest_path": compilation.manifest_path.relative_to(
                args.asset_root.resolve()
            ).as_posix(),
            "manifest_sha256": file_hash(compilation.manifest_path),
            "cache_hits": compilation.cache_hits,
            "cache_misses": compilation.cache_misses,
            "provider_calls": compilation.provider_calls,
        }
    if "cases" in load_yaml(scenario_path):
        suite, cases = load_suite(scenario_path, asset_root=args.asset_root)
        repetitions = args.repetitions if args.repetitions is not None else suite.repetitions
    else:
        cases, repetitions = (
            (load_scenario(scenario_path, asset_root=args.asset_root),),
            args.repetitions or 1,
        )
    if args.limit is not None:
        if args.limit < 1:
            parser.error("limit must be positive")
        cases = cases[: args.limit]
    output = args.output or Path("runs") / (
        cases[0].category
        + "_"
        + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ_")
        + uuid.uuid4().hex[:8]
    )
    config = SessionConfig.model_validate(load_yaml(args.config or registration.config_path))
    profile = LatencyProfile.model_validate(load_yaml(args.profile))
    result = asyncio.run(
        run_suite(
            output=output,
            scenarios=cases,
            source_root=args.asset_root,
            registration=registration,
            model_config=config,
            profile=profile,
            repetitions=repetitions,
            warmups=args.warmups,
            secrets=secrets,
            turn_mode=args.turn_mode,
            source_compilation=source_compilation,
        )
    )
    print(
        json.dumps(
            {
                "output": str(output),
                "evaluation_id": result["evaluation_id"],
                **result["realtime"]["counts"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
