"""Freeze Chinese voice Agent cases; answer oracles are never model inputs."""

import argparse
from pathlib import Path

import yaml

from scripts.prepare_interruption import MODEL, VOICE, freeze

CASES = (
    (
        "zh_agent_weather_001",
        "请查询二零二六年九月二十日上海的天气。",
        ("weather",),
        [{"tool": "weather", "arguments": {"city": "上海", "date": "2026-09-20"}}],
        {"calendar_events": []},
        [],
    ),
    (
        "zh_agent_calendar_001",
        "请在日历中创建一场标题为项目会议的活动，时间是二零二六年九月二十日上午十点到十一点。",
        ("calendar",),
        [
            {
                "tool": "calendar",
                "arguments": {
                    "operation": "create",
                    "title": "项目会议",
                    "start": "2026-09-20T10:00:00",
                    "end": "2026-09-20T11:00:00",
                },
            }
        ],
        {
            "calendar_events": [
                {
                    "event_id": "event_001",
                    "title": "项目会议",
                    "start": "2026-09-20T10:00:00",
                    "end": "2026-09-20T11:00:00",
                }
            ]
        },
        [],
    ),
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--transport", choices=["sdk", "stdlib"], default="sdk")
    args = parser.parse_args()
    if not 1 <= args.limit <= len(CASES):
        parser.error("invalid limit")
    audio_dir, scenario_dir = args.root / "datasets/audio/agent", args.root / "scenarios/agent"
    audio_dir.mkdir(parents=True, exist_ok=True)
    scenario_dir.mkdir(parents=True, exist_ok=True)
    for name, text, enabled, expected, state, failures in CASES[: args.limit]:
        wav = audio_dir / f"{name}.wav"
        meta = freeze(text, wav, audio_dir / f"{name}.json", transport=args.transport)
        scenario = {
            "schema_version": "0.1",
            "scenario_id": name,
            "scenario_version": 1,
            "world": {"now": "2026-09-19T10:00:00+08:00", "timezone": "Asia/Shanghai"},
            "user_turns": [text],
            "tools_enabled": list(enabled),
            "expected_calls": expected,
            "expected_final_state": state,
            "failure_schedule": failures,
            "turn_assets": ["question"],
            "audio_assets": {
                "question": {
                    "path": wav.relative_to(args.root).as_posix(),
                    "sha256": meta["sha256"],
                    "reference_text": text,
                    "speech_bounds_samples": meta["speech_bounds_samples"],
                    "sample_rate_hz": 16000,
                    "provenance": {"kind": "frozen_tts", "speaker_id": VOICE, "generator": MODEL},
                    "boundary_annotation": meta["boundary_annotation"],
                }
            },
        }
        path = scenario_dir / f"{name}.yaml"
        output = yaml.safe_dump(scenario, allow_unicode=True, sort_keys=False)
        if path.exists() and path.read_text() != output:
            raise ValueError("frozen scenario changed; choose a new version")
        path.write_text(output)
        print(path, flush=True)


if __name__ == "__main__":
    main()
