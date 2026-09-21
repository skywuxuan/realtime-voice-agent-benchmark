"""Freeze a two-utterance Chinese interruption fixture and scenario."""

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
import urllib.request
from pathlib import Path

import yaml

from benchmark.audio import AudioFormat
from simulator.audio import estimate_speech_bounds, read_wav

PROMPTS = {
    "initial": "请详细介绍北京适合周末游玩的地方，至少说三个，并说明各自的特点。",
    "interrupt": "等等，不是北京，我想问上海。",
}
MODEL, VOICE = "qwen3-tts-flash", "Cherry"


def freeze(text: str, wav_path: Path, meta_path: Path, *, transport="sdk") -> dict:
    if meta_path.exists():
        meta = json.loads(meta_path.read_text())
        if (
            meta["text"] != text
            or hashlib.sha256(wav_path.read_bytes()).hexdigest() != meta["sha256"]
        ):
            raise ValueError(f"existing fixture differs: {wav_path}")
        return meta
    if wav_path.exists():
        raise ValueError("unregistered WAV exists; choose a new asset path")
    if transport == "stdlib":
        from adapters.qwen.tts import synthesize

        encoded, request_metadata = synthesize(text, model=MODEL, voice=VOICE)
    elif transport == "sdk":
        import dashscope

        secret = os.environ.get("DASHSCOPE_API_KEY", "")
        if not secret:
            raise ValueError("DASHSCOPE_API_KEY is required")
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
        encoded = None
        for _ in range(3):
            try:
                with urllib.request.urlopen(response.output.audio.url, timeout=40) as stream:
                    encoded = stream.read()
                break
            except Exception:
                with tempfile.NamedTemporaryFile(
                    prefix="voice-bench-tts-url-", suffix=".bin", delete=False
                ) as temp:
                    temp_path = Path(temp.name)
                try:
                    subprocess.run(
                        [
                            "curl",
                            "-fsSL",
                            "--retry",
                            "2",
                            "--max-time",
                            "60",
                            response.output.audio.url,
                            "-o",
                            str(temp_path),
                        ],
                        check=True,
                    )
                    encoded = temp_path.read_bytes()
                    break
                finally:
                    temp_path.unlink(missing_ok=True)
        if not encoded:
            raise RuntimeError("TTS audio download failed")
        request_metadata = {"sdk_version": dashscope.__version__, "request_id": response.request_id}
    else:
        raise ValueError("unknown TTS transport")
    with tempfile.TemporaryDirectory(prefix="voice-bench-interruption-") as temp:
        source = Path(temp) / "source.wav"
        source.write_bytes(encoded)
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
        "text": text,
        "model": MODEL,
        "voice": VOICE,
        **request_metadata,
        "sha256": hashlib.sha256(wav_path.read_bytes()).hexdigest(),
        "source_audio_sha256": hashlib.sha256(encoded).hexdigest(),
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
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    return meta


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args()
    root = args.root.resolve()
    audio_dir = root / "datasets/audio/interruption"
    scenario_dir = root / "scenarios/realtime/interruption"
    audio_dir.mkdir(parents=True, exist_ok=True)
    scenario_dir.mkdir(parents=True, exist_ok=True)
    meta = {}
    for key, text in PROMPTS.items():
        meta[key] = freeze(
            text,
            audio_dir / f"zh_interruption_001_{key}.wav",
            audio_dir / f"zh_interruption_001_{key}.json",
        )
    scenario = {
        "schema_version": "0.1",
        "scenario_id": "zh_interruption_001",
        "scenario_version": 2,
        "suite": "realtime",
        "category": "interruption",
        "language": "zh-CN",
        "tags": ["clean", "frozen_tts", "automatic_boundary", "native_server_vad"],
        "seed": 17,
        "world": {"now": "2026-09-19T10:00:00+08:00", "timezone": "Asia/Shanghai"},
        "capabilities_required": [
            "audio_input",
            "audio_output",
            "streaming_input",
            "streaming_output",
            "server_vad",
        ],
        "session": {
            "system_prompt": "请用自然、简洁的中文交流。",
            "turn_mode": "server_vad",
            "control_profile": "native_server",
        },
        "audio": {"input_encoding": "pcm_s16le", "channels": 1, "chunk_ms": 20, "assets": {}},
        "actions": [],
        "oracle": {
            "hidden_from_model": True,
            "stimulus": "interruption",
            "expected_new_intent": {"city": "上海"},
            "forbidden_old_intent": {"city": "北京"},
            "metric_profile": "interruption_v0_3",
            "assertions": [
                {"type": "old_response_stops"},
                {"type": "answer_targets_city", "city": "上海"},
            ],
        },
        "termination": {
            "max_case_duration_ms": 90000,
            "response_timeout_ms": 20000,
            "post_stimulus_observation_ms": 20000,
            "drain_timeout_ms": 60000,
        },
    }
    for key, action_id, stimulus, turn_id in (
        ("initial", "ask", "utterance", "t1"),
        ("interrupt", "correct", "interruption", "t2"),
    ):
        path = (audio_dir / f"zh_interruption_001_{key}.wav").relative_to(root).as_posix()
        scenario["audio"]["assets"][key] = {
            "path": path,
            "sha256": meta[key]["sha256"],
            "reference_text": meta[key]["text"],
            "speech_bounds_samples": meta[key]["speech_bounds_samples"],
            "sample_rate_hz": 16000,
            "provenance": {"kind": "frozen_tts", "speaker_id": VOICE, "generator": MODEL},
            "boundary_annotation": meta[key]["boundary_annotation"],
        }
        action = {
            "action_id": action_id,
            "type": "play_audio",
            "asset": key,
            "turn_id": turn_id,
            "stimulus": stimulus,
            "intent_revision": 2 if stimulus == "interruption" else None,
        }
        if stimulus == "utterance":
            action["trigger"] = {"type": "session_ready"}
        else:
            action["trigger"] = {
                "type": "after_event",
                "event": "assistant_playback_start",
                "where": {"turn_id": "t1"},
                "occurrence": 1,
                "bind": {"target_response_id": "response_id"},
                "delay_ms": 350,
                "timeout_ms": 20000,
            }
            action["preconditions"] = [
                {"type": "response_still_playing", "response": "$target_response_id"},
                {"type": "response_still_generating", "response": "$target_response_id"},
                {"type": "minimum_continuation_evidence", "remaining_ms": 800},
            ]
        scenario["actions"].append(action)
    path = scenario_dir / "zh_interruption_001.yaml"
    path.write_text(yaml.safe_dump(scenario, allow_unicode=True, sort_keys=False))
    (scenario_dir.parent / "interruption_basic.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "0.1",
                "suite_id": "zh_interruption_basic_v1",
                "cases": ["interruption/zh_interruption_001.yaml"],
                "repetitions": 1,
            },
            allow_unicode=True,
            sort_keys=False,
        )
    )
    print(
        json.dumps(
            {"scenario": str(path.relative_to(root)), "assets": list(meta)}, ensure_ascii=False
        )
    )


if __name__ == "__main__":
    main()
