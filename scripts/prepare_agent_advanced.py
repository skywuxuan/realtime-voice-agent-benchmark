"""Freeze multi-step, transient-failure and in-flight correction voice cases."""

import argparse
from pathlib import Path

import yaml

from scenarios.loader import load_yaml
from scripts.prepare_interruption import MODEL, VOICE, freeze
from tools.scenarios import AgentScenario


def reference(field):
    return {"$result": {"step": "search", "path": ["trains", 0, field]}}


def specs():
    train = {"from_city": "上海", "to_city": "天津", "date": "2026-09-20"}
    return {
        "multistep": {
            "texts": [
                "请先查询二零二六年九月二十日上海到天津的高铁，再把查到的第一趟车的出发和到达时间建成一条日历活动，活动标题就用车次号。"
            ],
            "tools_enabled": ["train", "calendar"],
            "tags": ["multi_step"],
            "expected_calls": [
                {"step_id": "search", "tool": "train", "arguments": train},
                {
                    "step_id": "create",
                    "tool": "calendar",
                    "depends_on": ["search"],
                    "arguments": {
                        "operation": "create",
                        "title": reference("train_no"),
                        "start": reference("depart_at"),
                        "end": reference("arrive_at"),
                    },
                },
            ],
            "expected_final_state": {
                "calendar_events": [
                    {
                        "event_id": "event_001",
                        "title": "G2",
                        "start": "2026-09-20T08:12:00",
                        "end": "2026-09-20T12:04:00",
                    }
                ]
            },
        },
        "retry": {
            "texts": [],
            "reuse_weather": True,
            "tools_enabled": ["weather"],
            "tags": ["tool_failure"],
            "expected_calls": [
                {"tool": "weather", "arguments": {"city": "上海", "date": "2026-09-20"}}
            ],
            "expected_final_state": {"calendar_events": []},
            "failure_schedule": [{"tool": "weather", "invocation": 1, "kind": "http_500"}],
        },
        "correction": {
            "texts": [
                "请查询二零二六年九月二十日上海去北京的高铁。",
                "等一下，目的地不是北京，改成天津，日期不变。",
            ],
            "tools_enabled": ["train"],
            "tags": ["correction", "inflight_read_only"],
            "expected_calls": [
                {"tool": "train", "arguments": {**train, "to_city": "北京"}},
                {"tool": "train", "arguments": train},
            ],
            "expected_final_state": {"calendar_events": []},
            "turn_triggers": [
                {"type": "after_tool_start", "tool": "train", "occurrence": 1, "delay_ms": 150}
            ],
            "tool_delays": [{"tool": "train", "invocation": 1, "delay_ms": 8000}],
        },
    }


def persist(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    if path.exists() and path.read_text() != text:
        raise ValueError("frozen scenario changed; use a new version")
    path.write_text(text)


def prepare(root, transport):
    cases = []
    audio_dir = root / "datasets/audio/agent"
    audio_dir.mkdir(parents=True, exist_ok=True)
    for kind, spec in specs().items():
        name = f"zh_agent_{kind}_001"
        texts = spec.pop("texts")
        assets = {}
        turns = []
        if spec.pop("reuse_weather", False):
            source = load_yaml(root / "scenarios/agent/zh_agent_weather_001.yaml")
            assets = source["audio_assets"]
            turns = source["turn_assets"]
            texts = source["user_turns"]
        else:
            for i, text in enumerate(texts, 1):
                wav = audio_dir / f"{name}_t{i}.wav"
                meta = freeze(text, wav, audio_dir / f"{name}_t{i}.json", transport=transport)
                asset_name = f"t{i}"
                turns.append(asset_name)
                assets[asset_name] = {
                    "path": wav.relative_to(root).as_posix(),
                    "sha256": meta["sha256"],
                    "reference_text": text,
                    "speech_bounds_samples": meta["speech_bounds_samples"],
                    "sample_rate_hz": 16000,
                    "provenance": {"kind": "frozen_tts", "speaker_id": VOICE, "generator": MODEL},
                    "boundary_annotation": meta["boundary_annotation"],
                }
        data = {
            "schema_version": "0.1",
            "scenario_id": name,
            "scenario_version": 1,
            "world": {"now": "2026-09-19T10:00:00+08:00", "timezone": "Asia/Shanghai"},
            "user_turns": texts,
            "turn_assets": turns,
            "audio_assets": assets,
            "argument_comparison": "typed_iso8601",
            **spec,
        }
        AgentScenario.model_validate(data)
        persist(root / f"scenarios/agent/{name}.yaml", data)
        cases.append(f"{name}.yaml")
        print(name, flush=True)
    persist(
        root / "scenarios/agent/advanced.yaml",
        {
            "schema_version": "0.1",
            "suite_id": "agent_advanced_v1",
            "cases": cases,
            "repetitions": 1,
        },
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--transport", choices=["stdlib", "sdk"], default="stdlib")
    args = parser.parse_args()
    prepare(args.root, args.transport)


if __name__ == "__main__":
    main()
