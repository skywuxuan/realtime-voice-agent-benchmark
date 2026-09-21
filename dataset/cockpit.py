"""Compile cockpit protocol/test JSONL into frozen half-duplex Agent scenarios."""

import argparse
import collections
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import Field, JsonValue, field_validator

from adapters.base import ToolDefinition
from benchmark.contracts import Contract, Identifier, canonical_json, content_hash
from dataset.compiler import RenderCache, _write_immutable
from dataset.schema import TTSProfile
from events.replay import artifact_path, file_hash
from renderers.base import TTSRenderer
from renderers.registry import create_renderer
from scenarios.loader import load_yaml
from scenarios.schema import AudioAsset, Suite
from tools.catalog import ToolCatalog, ToolCatalogReference, validate_arguments
from tools.scenarios import AgentScenario

COCKPIT_COMPILER_VERSION = "cockpit-jsonl-compiler-0.1"
DEFAULT_TARGET_MODEL = "qwen-audio-3.0-realtime-flash"
COCKPIT_SYSTEM_PROMPT = """你是智能座舱语音助手，负责理解并执行用户明确提出的座舱操作。

规则：
- 对明确可执行且匹配所提供能力的请求，必须调用最匹配的一个工具真实执行，不得只用口头声称完成。
- 工具名称只能从当前提供的列表中选择，不得改名或创造不存在的工具。
- 严格依据用户原话抽取参数；枚举值遵循工具 schema，用户未表达的可选参数不要猜测或从示例复制。
- 当前评测每轮只有一个主要操作；不要调用无关工具。存在关键歧义时先简短追问。
- 只有收到工具成功结果后，才用一句自然、简短的中文确认结果；工具失败时如实说明。
- 普通闲聊、背景讨论、情绪表达和没有可执行意图的感叹不要触发工具。"""


class SourceParameter(Contract):
    description: str = ""
    define: dict[str, JsonValue]


class SourceFunction(Contract):
    name: Identifier
    description: str
    input_param: dict[str, SourceParameter] = Field(default_factory=dict)
    skill: str = ""
    classify: str = ""


class SourceFunctionResult(Contract):
    name: str
    param: dict[str, JsonValue] = Field(default_factory=dict)


class SourceCase(Contract):
    dlg_function: str = ""
    dlg_domain: str = ""
    case: str = Field(min_length=1)
    function_result: SourceFunctionResult

    @field_validator("case", mode="before")
    @classmethod
    def trimmed_text(cls, value):
        return value.strip() if isinstance(value, str) else value


@dataclass(frozen=True)
class IndexedCase:
    line_number: int
    case: SourceCase


@dataclass(frozen=True)
class CockpitCompilationResult:
    suite_path: Path
    scenario_paths: tuple[Path, ...]
    catalog_path: Path
    manifest_path: Path
    compilation_id: str
    cache_hits: int
    cache_misses: int
    provider_calls: int


def _read_jsonl(path: Path, model) -> tuple:
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append((number, model.model_validate_json(line)))
        except ValueError as error:
            raise ValueError(f"{path.name}:{number}: invalid JSONL record") from error
    if not rows:
        raise ValueError(f"{path.name}: JSONL is empty")
    return tuple(rows)


def _schema_from_define(define: dict[str, JsonValue]) -> dict[str, JsonValue]:
    allowed = {"type", "choice", "default", "items", "properties"}
    if set(define) - allowed:
        raise ValueError("unsupported protocol define field")
    source_type = define.get("type")
    target_type = {"float": "number"}.get(source_type, source_type)
    if target_type not in {"string", "integer", "number", "boolean", "array", "object"}:
        raise ValueError("unsupported protocol parameter type")
    schema: dict[str, JsonValue] = {"type": target_type}
    choices = define.get("choice", [])
    if not isinstance(choices, list):
        raise ValueError("protocol choice must be a list")
    if choices:
        schema["enum"] = choices
    if target_type == "array":
        items = define.get("items")
        schema["items"] = _schema_from_define(items) if isinstance(items, dict) else {}
    if target_type == "object":
        properties = define.get("properties", {})
        if not isinstance(properties, dict):
            raise ValueError("protocol object properties must be a mapping")
        schema["properties"] = {
            name: _parameter_schema(SourceParameter.model_validate(value))
            for name, value in properties.items()
        }
        schema["additionalProperties"] = False
    return schema


def _parameter_schema(parameter: SourceParameter) -> dict[str, JsonValue]:
    return {"description": parameter.description, **_schema_from_define(parameter.define)}


def load_protocol(path: Path) -> ToolCatalog:
    indexed = _read_jsonl(path, SourceFunction)
    functions = [row for _, row in indexed]
    names = [row.name for row in functions]
    if len(names) != len(set(names)):
        raise ValueError("protocol function names must be unique")
    source_hash = file_hash(path)
    catalog_id = f"cockpit_{path.stem}_{source_hash[:12]}"
    return ToolCatalog(
        catalog_id=catalog_id,
        source={
            "format": "common_func_jsonl_v1",
            "filename": path.name,
            "sha256": source_hash,
            "row_count": len(functions),
        },
        tools=tuple(
            ToolDefinition(
                name=row.name,
                description=row.description,
                parameters={
                    "type": "object",
                    "properties": {
                        name: _parameter_schema(parameter)
                        for name, parameter in row.input_param.items()
                    },
                    "additionalProperties": False,
                },
            )
            for row in functions
        ),
    )


def load_cases(path: Path) -> tuple[IndexedCase, ...]:
    return tuple(IndexedCase(number, row) for number, row in _read_jsonl(path, SourceCase))


def select_cases(
    rows: tuple[IndexedCase, ...],
    line_numbers: tuple[int, ...] = (),
    *,
    start_line: int | None = None,
    limit: int | None = None,
) -> tuple[IndexedCase, ...]:
    if line_numbers:
        if len(line_numbers) != len(set(line_numbers)):
            raise ValueError("provide unique one-based --case-line values")
        if start_line is not None or limit is not None:
            raise ValueError("case-line cannot be combined with start-line/limit")
        by_line = {row.line_number: row for row in rows}
        missing = set(line_numbers) - by_line.keys()
        if missing:
            raise ValueError(f"testset lines do not exist: {sorted(missing)}")
        selected = tuple(by_line[number] for number in line_numbers)
        if any(not row.case.function_result.name for row in selected):
            raise ValueError("no-tool cases need a separate benchmark profile")
        return selected
    if start_line is None or limit is None or start_line < 1 or limit < 1:
        raise ValueError("provide case-line values or positive start-line and limit")
    selected = tuple(
        row
        for row in rows
        if row.line_number >= start_line and row.case.function_result.name
    )[:limit]
    if len(selected) != limit:
        raise ValueError("not enough tool cases remain in the requested range")
    return selected


def audit_sources(catalog: ToolCatalog, rows: tuple[IndexedCase, ...]) -> dict:
    definitions = {tool.name: tool for tool in catalog.tools}
    failures = []
    valid = no_tool = 0
    by_function = collections.Counter()
    by_reason = collections.Counter()
    for row in rows:
        expected = row.case.function_result
        if not expected.name:
            no_tool += 1
            continue
        try:
            definition = definitions[expected.name]
            validate_arguments(definition.parameters, expected.param)
            valid += 1
        except (KeyError, TypeError, ValueError) as error:
            reason = "unknown expected function" if expected.name not in definitions else str(error)
            by_function[expected.name] += 1
            by_reason[reason] += 1
            failures.append(
                {
                    "line_number": row.line_number,
                    "function": expected.name,
                    "reason": reason,
                    "arguments": expected.param,
                }
            )
    return {
        "schema_version": "0.1",
        "kind": "cockpit_source_audit",
        "catalog_id": catalog.catalog_id,
        "counts": {
            "total": len(rows),
            "tool_cases": len(rows) - no_tool,
            "valid_tool_cases": valid,
            "invalid_tool_cases": len(failures),
            "no_tool_cases": no_tool,
        },
        "invalid_by_function": dict(sorted(by_function.items())),
        "invalid_by_reason": dict(sorted(by_reason.items())),
        "invalid_cases": failures,
    }


def _compiler_fingerprint() -> dict[str, str]:
    root = Path(__file__).parents[1]
    files = (
        "dataset/cockpit.py",
        "dataset/compiler.py",
        "tools/catalog.py",
        "tools/scenarios.py",
        "agent/runtime.py",
        "agent/evaluate.py",
    )
    return {name: file_hash(root / name) for name in files}


def compile_cockpit_dataset(
    *,
    protocol_path: Path,
    testset_path: Path,
    case_lines: tuple[int, ...] = (),
    start_line: int | None = None,
    limit: int | None = None,
    dataset_id: str,
    profile: TTSProfile,
    renderer: TTSRenderer,
    asset_root: Path,
    render_root: Path = Path("datasets/rendered"),
    catalog_root: Path = Path("datasets/catalogs/cockpit"),
    compiled_root: Path = Path("scenarios/agent/cockpit_compiled"),
    allow_render: bool,
    target_model: str = DEFAULT_TARGET_MODEL,
    input_chunk_ms: int = 200,
    expose_all_tools: bool = False,
    secrets: tuple[str, ...] = (),
) -> CockpitCompilationResult:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", dataset_id):
        raise ValueError("dataset_id must be a safe path component")
    asset_root = asset_root.resolve()
    catalog = load_protocol(protocol_path)
    rows = load_cases(testset_path)
    selected = select_cases(rows, case_lines, start_line=start_line, limit=limit)
    definitions = {tool.name: tool for tool in catalog.tools}
    for row in selected:
        expected = row.case.function_result
        if expected.name not in definitions:
            raise ValueError(f"testset line {row.line_number}: unknown expected function")
        try:
            validate_arguments(definitions[expected.name].parameters, expected.param)
        except ValueError as error:
            raise ValueError(
                f"testset line {row.line_number}: expected arguments violate protocol"
            ) from error
    selected_tool_names = tuple(
        dict.fromkeys(row.case.function_result.name for row in selected)
    )

    catalog_directory = artifact_path(asset_root, catalog_root.as_posix())
    catalog_path = catalog_directory / f"{catalog.catalog_id}.json"
    _write_immutable(
        catalog_path,
        (canonical_json(catalog.model_dump(mode="json")) + "\n").encode("utf-8"),
    )
    catalog_reference = ToolCatalogReference(
        catalog_id=catalog.catalog_id,
        path=catalog_path.relative_to(asset_root).as_posix(),
        sha256=file_hash(catalog_path),
    )
    cache = RenderCache(
        artifact_path(asset_root, render_root.as_posix()),
        profile,
        renderer,
        allow_render=allow_render,
        secrets=secrets,
    )
    fingerprints = _compiler_fingerprint()
    source_snapshot = {
        "schema_version": "0.1",
        "kind": "cockpit_source_selection",
        "protocol": catalog.source,
        "testset": {
            "filename": testset_path.name,
            "sha256": file_hash(testset_path),
            "row_count": len(rows),
        },
        "selected": [
            {"line_number": row.line_number, **row.case.model_dump(mode="json")}
            for row in selected
        ],
    }
    compilation_id = content_hash(
        {
            "compiler_version": COCKPIT_COMPILER_VERSION,
            "compiler_fingerprint": fingerprints,
            "dataset_id": dataset_id,
            "source": source_snapshot,
            "catalog_sha256": catalog_reference.sha256,
            "tts_profile_sha256": cache.profile_hash,
            "target_model": target_model,
            "system_prompt": COCKPIT_SYSTEM_PROMPT,
            "input_chunk_ms": input_chunk_ms,
            "exposed_tools": "all" if expose_all_tools else selected_tool_names,
        }
    )[:20]
    directory = artifact_path(asset_root, compiled_root.as_posix()) / dataset_id / compilation_id
    scenario_paths = []
    for row in selected:
        rendered = cache.render_text(row.case.case)
        asset = AudioAsset(
            path=rendered.wav_path.relative_to(asset_root).as_posix(),
            sha256=file_hash(rendered.wav_path),
            reference_text=row.case.case,
            speech_bounds_samples=rendered.speech_bounds_samples,
            sample_rate_hz=profile.sample_rate_hz,
            provenance={
                "kind": "frozen_tts",
                "speaker_id": profile.voice,
                "generator": profile.model,
            },
            boundary_annotation=rendered.boundary_annotation,
            derivation={
                "renderer_profile_id": profile.profile_id,
                "renderer_profile_sha256": cache.profile_hash,
                "render_id": rendered.render_id,
                "metadata_path": rendered.metadata_path.relative_to(asset_root).as_posix(),
                "source_testset_sha256": source_snapshot["testset"]["sha256"],
                "source_line_number": row.line_number,
            },
        )
        expected = row.case.function_result
        scenario_id = f"cockpit_{row.line_number:06d}_{expected.name}"
        scenario = AgentScenario(
            scenario_id=scenario_id,
            scenario_version=1,
            model=target_model,
            world={
                "source_testset": testset_path.name,
                "source_line_number": row.line_number,
            },
            user_turns=(row.case.case,),
            tools_enabled=() if expose_all_tools else selected_tool_names,
            tool_backend="protocol_ack_v1",
            tool_catalog=catalog_reference,
            expected_calls=({"tool": expected.name, "arguments": expected.param},),
            tags=("cockpit", "half_duplex", "single_tool", "tts_rendered"),
            audio_assets={"t1": asset},
            turn_assets=("t1",),
            allow_retries=False,
            max_tool_calls=4,
            response_timeout_s=60,
            drain_timeout_s=60,
            system_prompt=COCKPIT_SYSTEM_PROMPT,
            input_chunk_ms=input_chunk_ms,
        )
        path = directory / f"{scenario_id}.yaml"
        _write_immutable(
            path,
            yaml.safe_dump(
                scenario.model_dump(mode="json"), allow_unicode=True, sort_keys=True
            ).encode("utf-8"),
        )
        scenario_paths.append(path)
    suite = Suite(
        schema_version="0.1",
        suite_id=f"{dataset_id}_{compilation_id}",
        cases=tuple(path.name for path in scenario_paths),
        repetitions=1,
    )
    suite_path = directory / "suite.yaml"
    source_path = directory / "source.json"
    _write_immutable(
        suite_path,
        yaml.safe_dump(suite.model_dump(mode="json"), allow_unicode=True, sort_keys=True).encode(
            "utf-8"
        ),
    )
    _write_immutable(
        source_path, (canonical_json(source_snapshot) + "\n").encode("utf-8")
    )
    manifest = {
        "schema_version": "0.1",
        "kind": "compiled_cockpit_dataset",
        "compiler_version": COCKPIT_COMPILER_VERSION,
        "compiler_fingerprint": fingerprints,
        "compilation_id": compilation_id,
        "dataset_id": dataset_id,
        "target_model": target_model,
        "exposed_tool_count": len(catalog.tools) if expose_all_tools else len(selected_tool_names),
        "exposed_tool_scope": "all" if expose_all_tools else "selected_cases",
        "catalog": catalog_reference.model_dump(mode="json"),
        "tts_profile": profile.model_dump(mode="json"),
        "renderer_fingerprint": cache.fingerprint,
        "renderer_profile_sha256": cache.profile_hash,
        "suite": suite_path.name,
        "files": {
            path.relative_to(directory).as_posix(): file_hash(path)
            for path in (*scenario_paths, suite_path, source_path)
        },
    }
    manifest_path = directory / "manifest.json"
    _write_immutable(manifest_path, (canonical_json(manifest) + "\n").encode("utf-8"))
    return CockpitCompilationResult(
        suite_path=suite_path,
        scenario_paths=tuple(scenario_paths),
        catalog_path=catalog_path,
        manifest_path=manifest_path,
        compilation_id=compilation_id,
        cache_hits=cache.cache_hits,
        cache_misses=cache.cache_misses,
        provider_calls=cache.provider_calls,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--testset", type=Path, required=True)
    parser.add_argument("--case-line", type=int, action="append")
    parser.add_argument("--start-line", type=int)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dataset-id", default="cockpit_audio3_flash_smoke_v1")
    parser.add_argument("--target-model", default=DEFAULT_TARGET_MODEL)
    parser.add_argument("--input-chunk-ms", type=int, default=200)
    parser.add_argument("--expose-all-tools", action="store_true")
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--audit-output", type=Path)
    parser.add_argument(
        "--tts-profile",
        type=Path,
        default=Path(__file__).parents[1] / "configs/tts/qwen-cherry.yaml",
    )
    parser.add_argument("--asset-root", type=Path, default=Path("."))
    parser.add_argument("--render-root", type=Path, default=Path("datasets/rendered"))
    parser.add_argument("--catalog-root", type=Path, default=Path("datasets/catalogs/cockpit"))
    parser.add_argument(
        "--compiled-root", type=Path, default=Path("scenarios/agent/cockpit_compiled")
    )
    parser.add_argument("--render-missing", action="store_true")
    args = parser.parse_args()
    if args.audit_only:
        audit = audit_sources(load_protocol(args.protocol), load_cases(args.testset))
        payload = canonical_json(audit) + "\n"
        if args.audit_output:
            args.audit_output.parent.mkdir(parents=True, exist_ok=True)
            args.audit_output.write_text(payload, encoding="utf-8")
            print(
                json.dumps(
                    {"output": str(args.audit_output), **audit["counts"]},
                    ensure_ascii=False,
                )
            )
        else:
            print(payload, end="")
        return
    profile = TTSProfile.model_validate(load_yaml(args.tts_profile))
    renderer = create_renderer(profile)
    secret = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    try:
        result = compile_cockpit_dataset(
            protocol_path=args.protocol,
            testset_path=args.testset,
            case_lines=tuple(args.case_line or ()),
            start_line=args.start_line,
            limit=args.limit,
            dataset_id=args.dataset_id,
            profile=profile,
            renderer=renderer,
            asset_root=args.asset_root,
            render_root=args.render_root,
            catalog_root=args.catalog_root,
            compiled_root=args.compiled_root,
            allow_render=args.render_missing,
            target_model=args.target_model,
            input_chunk_ms=args.input_chunk_ms,
            expose_all_tools=args.expose_all_tools,
            secrets=(secret,) if secret else (),
        )
    except ValueError as error:
        parser.error(str(error))
    print(
        json.dumps(
            {
                "compilation_id": result.compilation_id,
                "suite": str(result.suite_path),
                "catalog": str(result.catalog_path),
                "scenarios": [str(path) for path in result.scenario_paths],
                "cache_hits": result.cache_hits,
                "cache_misses": result.cache_misses,
                "provider_calls": result.provider_calls,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
