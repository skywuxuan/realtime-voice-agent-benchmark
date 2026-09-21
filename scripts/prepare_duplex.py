"""Freeze pause and speech-overlap variants from existing Chinese TTS; no network."""

import argparse
import array
import hashlib
import sys
from copy import deepcopy
from pathlib import Path

import yaml

from benchmark.audio import AudioFormat
from scenarios.loader import load_scenario, load_yaml
from simulator.audio import read_wav_bytes, wav_bytes

PAUSE_MS = (200, 500, 800, 1200, 2000)


def persist(path, data):
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError(f"frozen asset changed; choose a new version: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def yaml_bytes(data):
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False).encode()


def prepare(root: Path):
    base = load_yaml(root / "scenarios/realtime/latency/zh_latency_003.yaml")
    target = deepcopy(base["audio"]["assets"]["question"])
    source = (root / target["path"]).read_bytes()
    if hashlib.sha256(source).hexdigest() != target["sha256"]:
        raise ValueError("input source hash mismatch")
    format = AudioFormat(sample_rate_hz=target["sample_rate_hz"])
    pcm = read_wav_bytes(source, format)
    prepared = []

    def save(name, category, data, asset):
        scenario = deepcopy(base)
        scenario.update(scenario_id=name, scenario_version=1, category=category)
        scenario["tags"] = [
            "clean" if category != "overlap" else "mixed_speech",
            "frozen_tts",
            "automatic_boundary",
            "derived_audio",
        ]
        scenario["oracle"].update(stimulus=category, metric_profile="duplex_v0_2")
        asset["path"] = f"datasets/audio/duplex/{name}.wav"
        asset["sha256"] = hashlib.sha256(data).hexdigest()
        scenario["audio"]["assets"] = {"question": asset}
        scenario_path = Path(f"scenarios/realtime/{category}/{name}.yaml")
        persist(root / asset["path"], data)
        persist(root / scenario_path, yaml_bytes(scenario))
        load_scenario(root / scenario_path, asset_root=root)
        prepared.append((category, scenario_path.relative_to("scenarios/realtime").as_posix()))

    clean = deepcopy(target)
    clean["derivation"] = {
        "operation": "identity",
        "source_path": target["path"],
        "source_sha256": target["sha256"],
    }
    save("zh_turn_taking_001", "turn_taking", source, clean)
    start, end = target["speech_bounds_samples"]
    cut = ((start + end) // 2 // 320) * 320
    for ms in PAUSE_MS:
        samples = ms * format.sample_rate_hz // 1000
        data = wav_bytes(pcm[: cut * 2] + b"\0\0" * samples + pcm[cut * 2 :], format)
        asset = deepcopy(target)
        asset["speech_bounds_samples"] = [start, end + samples]
        asset["regions"] = [
            {"region_id": "pause_1", "kind": "pause", "bounds_samples": [cut, cut + samples]}
        ]
        asset["derivation"] = {
            "operation": "insert_silence",
            "source_path": target["path"],
            "source_sha256": target["sha256"],
            "insert_sample": cut,
            "inserted_samples": samples,
            "natural_pause": False,
        }
        save(f"zh_pause_{ms:04d}", "pause", data, asset)
    background = load_yaml(root / "scenarios/realtime/latency/zh_latency_002.yaml")["audio"][
        "assets"
    ]["question"]
    source_bg = (root / background["path"]).read_bytes()
    if hashlib.sha256(source_bg).hexdigest() != background["sha256"]:
        raise ValueError("background source hash mismatch")
    bg_pcm = read_wav_bytes(source_bg, format)
    main, bg = array.array("h", pcm), array.array("h", bg_pcm)
    if sys.byteorder != "little":
        main.byteswap()
        bg.byteswap()
    for subtype, offset, gain in (
        ("ambient_speech", 0, 0.15),
        ("side_conversation", 1600, 0.3),
        ("simultaneous_speech", 0, 0.5),
    ):
        mixed = array.array("h", [0]) * max(len(main), offset + len(bg))
        for i in range(len(mixed)):
            value = (main[i] * (1 - gain) if i < len(main) else 0) + (
                bg[i - offset] * gain if offset <= i < offset + len(bg) else 0
            )
            mixed[i] = max(-32768, min(32767, round(value)))
        if sys.byteorder != "little":
            mixed.byteswap()
        asset = deepcopy(target)
        asset["regions"] = [
            {
                "region_id": "background_1",
                "kind": "interferer",
                "subtype": subtype,
                "bounds_samples": [v + offset for v in background["speech_bounds_samples"]],
            }
        ]
        asset["derivation"] = {
            "operation": "mix_pcm16",
            "target_path": target["path"],
            "target_sha256": target["sha256"],
            "interferer_path": background["path"],
            "interferer_sha256": background["sha256"],
            "target_gain": 1 - gain,
            "interferer_gain": gain,
            "interferer_offset_samples": offset,
            "recipe_version": "linear_gain_round_clip_v1",
            "speaker_variation": False,
        }
        save("zh_overlap_" + subtype, "overlap", wav_bytes(mixed.tobytes(), format), asset)
    for category in sorted({c for c, _ in prepared}):
        suite = {
            "schema_version": "0.1",
            "suite_id": "zh_" + category + "_v1",
            "cases": [p for c, p in prepared if c == category],
            "repetitions": 1,
        }
        persist(root / f"scenarios/realtime/{category}_basic.yaml", yaml_bytes(suite))
    return prepared


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args()
    for category, path in prepare(args.root):
        print(category, path)


if __name__ == "__main__":
    main()
