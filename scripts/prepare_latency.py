"""Generate and freeze ten Chinese TTS fixtures before (never during) a benchmark run."""

import argparse
import hashlib
import json
import logging
import os
import subprocess
import tempfile
import urllib.request
from pathlib import Path

import yaml

from benchmark.audio import AudioFormat
from simulator.audio import estimate_speech_bounds, read_wav

PROMPTS = [
    "你好，请问你叫什么名字？",
    "一加一等于几？",
    "请用一句话介绍北京。",
    "请告诉我，熊猫通常吃什么？",
    "请把英语单词 hello 翻译成中文。",
    "请说一句早上好的问候。",
    "请说出三种常见的水果。",
    "一年有多少个月？",
    "请用一句话解释什么是图书馆。",
    "谢谢你的帮助，请和我说再见。",
]
MODEL, VOICE = "qwen3-tts-flash", "Cherry"
SOURCE = "https://github.com/dashscope/dashscope-sdk-python/blob/b0b4469e13dd1b0c1d842f99fbfc6a300c5dc760/samples/test_qwen_tts.py"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    if not 1 <= args.limit <= 10:
        parser.error("limit must be between 1 and 10")
    secret = os.environ.get("DASHSCOPE_API_KEY", "")
    if not secret:
        parser.error("DASHSCOPE_API_KEY is required for asset preparation")
    import dashscope

    logging.getLogger("dashscope").setLevel(logging.CRITICAL)
    root = args.root.resolve()
    asset_dir, scenario_dir = root / "datasets/audio/latency", root / "scenarios/realtime/latency"
    asset_dir.mkdir(parents=True, exist_ok=True)
    scenario_dir.mkdir(parents=True, exist_ok=True)
    cases, metadata = [], []
    for index, text in enumerate(PROMPTS[: args.limit], 1):
        scenario_id = f"zh_latency_{index:03}"
        wav_path, meta_path = asset_dir / f"{scenario_id}.wav", asset_dir / f"{scenario_id}.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
            if (
                meta["text"] != text
                or hashlib.sha256(wav_path.read_bytes()).hexdigest() != meta["sha256"]
            ):
                raise ValueError(
                    "existing frozen asset differs; choose a new version, do not overwrite"
                )
        else:
            if wav_path.exists():
                raise ValueError("unregistered audio already exists; refusing to overwrite")
            response = dashscope.MultiModalConversation.call(
                api_key=secret,
                model=MODEL,
                text=text,
                voice=VOICE,
                language_type="Chinese",
                stream=False,
            )
            if response.status_code != 200 or response.output is None:
                raise RuntimeError(f"TTS failed: HTTP {response.status_code}, code={response.code}")
            # Provider output URL is used only for retrieval; signed URLs are not logged or saved.
            with urllib.request.urlopen(response.output.audio.url, timeout=40) as stream:
                audio = stream.read()
            with tempfile.TemporaryDirectory(prefix="voice-bench-tts-") as temp:
                source = Path(temp) / "source.wav"
                source.write_bytes(audio)
                subprocess.run(
                    [
                        "ffmpeg",
                        "-v",
                        "error",
                        "-i",
                        str(source),
                        "-ar",
                        "16000",
                        "-ac",
                        "1",
                        "-c:a",
                        "pcm_s16le",
                        str(wav_path),
                    ],
                    check=True,
                )
            pcm = read_wav(wav_path, AudioFormat(sample_rate_hz=16000))
            bounds, parameters = estimate_speech_bounds(pcm, 16000)
            meta = {
                "scenario_id": scenario_id,
                "text": text,
                "model": MODEL,
                "voice": VOICE,
                "sdk_version": dashscope.__version__,
                "source": SOURCE,
                "request_id": response.request_id,
                "sha256": hashlib.sha256(wav_path.read_bytes()).hexdigest(),
                "source_audio_sha256": hashlib.sha256(audio).hexdigest(),
                "sample_rate_hz": 16000,
                "sample_count": len(pcm) // 2,
                "speech_bounds_samples": list(bounds),
                "boundary_annotation": {
                    "method": "energy_rms_v1",
                    "status": "automatic",
                    "resolution_ms": 20,
                    "parameters": parameters,
                },
            }
            meta_path.write_text(
                json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        case = {
            "schema_version": "0.1",
            "scenario_id": scenario_id,
            "scenario_version": 1,
            "suite": "realtime",
            "category": "latency",
            "language": "zh-CN",
            "tags": ["clean", "frozen_tts", "automatic_boundary"],
            "seed": 17,
            "world": {"now": "2026-09-19T10:00:00+08:00", "timezone": "Asia/Shanghai"},
            "capabilities_required": [
                "audio_input",
                "audio_output",
                "streaming_output",
                "server_vad",
            ],
            "session": {
                "system_prompt": "请用一句简短、自然的中文回答用户。",
                "turn_mode": "server_vad",
                "control_profile": "native_server",
            },
            "audio": {
                "input_encoding": "pcm_s16le",
                "channels": 1,
                "chunk_ms": 20,
                "assets": {
                    "question": {
                        "path": wav_path.relative_to(root).as_posix(),
                        "sha256": meta["sha256"],
                        "reference_text": text,
                        "speech_bounds_samples": meta["speech_bounds_samples"],
                        "sample_rate_hz": 16000,
                        "provenance": {
                            "kind": "frozen_tts",
                            "speaker_id": VOICE,
                            "generator": MODEL,
                        },
                        "boundary_annotation": meta["boundary_annotation"],
                    }
                },
            },
            "actions": [
                {
                    "action_id": "ask",
                    "type": "play_audio",
                    "asset": "question",
                    "turn_id": "t1",
                    "trigger": {"type": "session_ready"},
                }
            ],
            "oracle": {
                "hidden_from_model": True,
                "stimulus": "utterance",
                "metric_profile": "latency_v0_1",
                "assertions": [{"type": "audio_received"}],
            },
            "termination": {
                "max_case_duration_ms": 60000,
                "response_timeout_ms": 15000,
                "post_stimulus_observation_ms": 5000,
                "drain_timeout_ms": 15000,
            },
        }
        case_path = scenario_dir / f"{scenario_id}.yaml"
        case_path.write_text(
            yaml.safe_dump(case, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        cases.append(f"latency/{scenario_id}.yaml")
        metadata.append(meta)
        print(
            json.dumps(
                {
                    "prepared": scenario_id,
                    "samples": meta["sample_count"],
                    "boundary": "automatic_energy_estimate",
                }
            ),
            flush=True,
        )
    (scenario_dir.parent / "basic.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "0.1",
                "suite_id": "zh_latency_basic_v1",
                "cases": cases,
                "repetitions": 1,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (asset_dir / "manifest.json").write_text(
        json.dumps(
            {"version": 1, "model": MODEL, "voice": VOICE, "cases": metadata},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
