"""Prepare frozen Chinese listener speech; historical synthetic tones stay separate."""

import argparse
from copy import deepcopy
from pathlib import Path

import yaml

from scenarios.loader import load_scenario, load_yaml
from scripts.prepare_interruption import MODEL, VOICE, freeze

PHRASES = ("嗯嗯", "你继续")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--transport", choices=["sdk", "stdlib"], default="stdlib")
    args = parser.parse_args()
    root = args.root
    base = load_yaml(root / "scenarios/realtime/interruption/zh_interruption_001.yaml")
    directory = root / "datasets/audio/backchannel"
    directory.mkdir(parents=True, exist_ok=True)
    cases = []
    for i, text in enumerate(PHRASES, 1):
        name = f"zh_backchannel_tts_{i:03d}"
        meta = freeze(
            text, directory / f"{name}.wav", directory / f"{name}.json", transport=args.transport
        )
        scenario = deepcopy(base)
        scenario.update(
            scenario_id=name,
            scenario_version=1,
            category="backchannel",
            tags=["clean", "frozen_tts", "automatic_boundary", "listener_backchannel"],
        )
        initial = scenario["audio"]["assets"]["initial"]
        scenario["audio"]["assets"] = {
            "initial": initial,
            "listener": {
                "path": (directory / f"{name}.wav").relative_to(root).as_posix(),
                "sha256": meta["sha256"],
                "reference_text": text,
                "speech_bounds_samples": meta["speech_bounds_samples"],
                "sample_rate_hz": 16000,
                "provenance": {"kind": "frozen_tts", "speaker_id": VOICE, "generator": MODEL},
                "boundary_annotation": meta["boundary_annotation"],
            },
        }
        scenario["actions"][1].update(
            action_id="ack", asset="listener", stimulus="backchannel", intent_revision=None
        )
        scenario["oracle"] = {
            "hidden_from_model": True,
            "stimulus": "backchannel",
            "metric_profile": "backchannel_v0_2",
            "assertions": [{"type": "continues_response"}],
        }
        path = root / f"scenarios/realtime/backchannel/{name}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        output = yaml.safe_dump(scenario, allow_unicode=True, sort_keys=False)
        if path.exists() and path.read_text() != output:
            raise ValueError("frozen backchannel scenario changed")
        path.write_text(output)
        load_scenario(path, asset_root=root)
        cases.append(f"backchannel/{name}.yaml")
        print(path, flush=True)
    suite = {
        "schema_version": "0.1",
        "suite_id": "zh_backchannel_tts_v1",
        "cases": cases,
        "repetitions": 1,
    }
    path = root / "scenarios/realtime/backchannel_tts.yaml"
    output = yaml.safe_dump(suite, allow_unicode=True, sort_keys=False)
    if path.exists() and path.read_text() != output:
        raise ValueError("frozen suite changed")
    path.write_text(output)


if __name__ == "__main__":
    main()
