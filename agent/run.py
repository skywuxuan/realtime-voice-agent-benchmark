"""Voice Agent single-case and suite execution through the unified adapter."""

import argparse
import asyncio
import os
import re
import uuid
from pathlib import Path

from adapters.base import SessionConfig
from adapters.registry import resolve_adapter
from agent.evaluate import evaluate_suite
from agent.runtime import run_agent_case
from benchmark.config import LatencyProfile
from benchmark.contracts import canonical_json
from benchmark.run import implementation_hash, write_index
from events.redaction import Redactor
from events.replay import artifact_path, file_hash
from events.schema import RecordingContext
from scenarios.loader import load_yaml
from scenarios.schema import Suite
from tools.catalog import ToolCatalog
from tools.scenarios import AgentScenario


def load_inputs(path, *, asset_root=Path(".")):
    data = load_yaml(path)
    if "cases" in data:
        suite = Suite.model_validate(data)
        cases = tuple(
            AgentScenario.model_validate(load_yaml(artifact_path(path.parent, name)))
            for name in suite.cases
        )
        repetitions = suite.repetitions
    else:
        cases = (AgentScenario.model_validate(data),)
        repetitions = 1
    if len({s.scenario_id for s in cases}) != len(cases):
        raise ValueError("duplicate agent scenario_id")
    for case in cases:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", case.scenario_id):
            raise ValueError("unsafe agent scenario_id")
        if len(case.turn_assets) != len(case.user_turns):
            raise ValueError(
                "every user turn needs a frozen WAV; expected_calls is evaluation-only"
            )
        if case.tool_catalog:
            catalog_path = artifact_path(asset_root, case.tool_catalog.path)
            if file_hash(catalog_path) != case.tool_catalog.sha256:
                raise ValueError("agent tool catalog hash mismatch")
            catalog = ToolCatalog.model_validate_json(catalog_path.read_text(encoding="utf-8"))
            if catalog.catalog_id != case.tool_catalog.catalog_id:
                raise ValueError("agent tool catalog ID mismatch")
            catalog.definitions(case.tools_enabled)
    return cases, repetitions


def load_tool_catalog(scenario, source_root):
    if scenario.tool_catalog is None:
        return None
    path = artifact_path(source_root, scenario.tool_catalog.path)
    if file_hash(path) != scenario.tool_catalog.sha256:
        raise ValueError("agent tool catalog hash mismatch")
    catalog = ToolCatalog.model_validate_json(path.read_text(encoding="utf-8"))
    if catalog.catalog_id != scenario.tool_catalog.catalog_id:
        raise ValueError("agent tool catalog ID mismatch")
    return catalog


async def run_suite(
    *,
    output,
    scenarios,
    source_root,
    registration,
    model_config,
    profile,
    repetitions=1,
    warmups=0,
    secrets=(),
):
    if not scenarios or repetitions < 1 or warmups < 0:
        raise ValueError("invalid agent suite plan")
    if any(
        scenario.model not in {"fixture", model_config.model} for scenario in scenarios
    ):
        raise ValueError("agent scenario target model differs from the run config")
    catalogs = {
        scenario.scenario_id: load_tool_catalog(scenario, source_root) for scenario in scenarios
    }
    run_id = "agent_" + uuid.uuid4().hex
    output.mkdir(parents=True, exist_ok=False)
    fingerprint = implementation_hash()
    manifest = {
        "kind": "agent_run",
        "schema_version": "0.1",
        "status": "running",
        "run_id": run_id,
        "adapter": registration.alias,
        "implementation_sha256": fingerprint,
        "attempts": [],
    }
    write_index(output, manifest)
    clean, _ = Redactor(secrets).clean(
        {
            "model_config": model_config.model_dump(mode="json"),
            "profile": profile.model_dump(mode="json"),
            "repetitions": repetitions,
            "warmups": warmups,
        }
    )
    (output / "config.json").write_text(canonical_json(clean) + "\n")
    jobs = [(scenarios[0], f"warmup_{i:03d}", True) for i in range(1, warmups + 1)]
    jobs.extend(
        (s, f"attempt_{r:03d}", False) for r in range(1, repetitions + 1) for s in scenarios
    )
    for scenario, attempt, warmup in jobs:
        context = RecordingContext(
            run_id=run_id,
            scenario_id=scenario.scenario_id,
            attempt_id=attempt,
            session_id="session_" + uuid.uuid4().hex,
        )
        path = f"cases/{scenario.scenario_id}/{attempt}"
        trial = await run_agent_case(
            registration.factory(model_config),
            scenario=scenario,
            source_wavs={
                a.path: artifact_path(source_root, a.path).read_bytes()
                for a in scenario.audio_assets.values()
            },
            output=output / path,
            context=context,
            config=model_config,
            profile=profile,
            secrets=secrets,
            implementation_hash=fingerprint,
            warmup=warmup,
            tool_catalog=catalogs[scenario.scenario_id],
        )
        manifest["attempts"].append(
            {
                "path": path,
                "scenario_id": scenario.scenario_id,
                "attempt_id": attempt,
                "warmup": warmup,
                "status": trial["status"],
                "manifest_sha256": file_hash(output / path / "manifest.json"),
            }
        )
        write_index(output, manifest)
        print(
            {
                "scenario_id": scenario.scenario_id,
                "attempt": attempt,
                "status": trial["status"],
                "reason": trial["reason"],
            },
            flush=True,
        )
    manifest["status"] = "complete"
    write_index(output, manifest)
    return evaluate_suite(output)


def run_cli(args):
    cases, repetitions = load_inputs(args.scenario, asset_root=args.asset_root)
    if args.limit:
        if args.limit < 1:
            raise ValueError("limit must be positive")
        cases = cases[: args.limit]
    registration = resolve_adapter(args.model)
    secrets = tuple(os.environ.get(name, "").strip() for name in registration.credential_variables)
    if any(not value for value in secrets):
        raise ValueError("adapter credential environment variable missing")
    config = SessionConfig.model_validate(load_yaml(args.config or registration.config_path))
    if getattr(args, "turn_mode", None):
        config = config.model_copy(update={"turn_mode": args.turn_mode})
    result = asyncio.run(
        run_suite(
            output=args.output or Path("runs") / ("agent_" + uuid.uuid4().hex),
            scenarios=cases,
            source_root=args.asset_root,
            registration=registration,
            model_config=config,
            profile=LatencyProfile.model_validate(load_yaml(args.profile)),
            repetitions=args.repetitions or repetitions,
            warmups=args.warmups,
            secrets=secrets,
        )
    )
    print({"evaluation_id": result["evaluation_id"], **result["agent"]["counts"]})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--asset-root", type=Path, default=Path("."))
    parser.add_argument("--model", default="qwen-realtime")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--profile", type=Path, default=Path("configs/latency.yaml"))
    parser.add_argument("--repetitions", type=int)
    parser.add_argument("--warmups", type=int, default=0)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    if args.repetitions is not None and args.repetitions < 1 or args.warmups < 0:
        parser.error("positive repetitions and nonnegative warmups required")
    run_cli(args)


if __name__ == "__main__":
    main()
