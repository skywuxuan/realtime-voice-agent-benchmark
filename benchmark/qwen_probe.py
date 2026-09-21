"""Qwen-specific composition root for Phase 2. The probe loop contains no vendor protocol."""

import argparse
import asyncio
import json
import os
from pathlib import Path

from adapters.base import SessionConfig
from adapters.qwen import QwenRealtimeAdapter, QwenSettings
from adapters.qwen.config import INPUT_FORMAT, OUTPUT_FORMAT
from benchmark.connection_probe import run_connection_probe


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--audio", type=Path, required=True, help="Mono PCM16 WAV, 16 kHz, at most 30 seconds"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="qwen3.5-omni-flash-realtime")
    parser.add_argument("--voice", default="Tina")
    parser.add_argument("--turn-mode", choices=["manual", "server_vad"], default="manual")
    parser.add_argument("--cancel-after-chunks", type=int)
    parser.add_argument("--audio-source", default="user_supplied_audio")
    args = parser.parse_args()
    secret = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if not secret:
        parser.error("DASHSCOPE_API_KEY must be set; this command makes a real API request")
    settings = QwenSettings(model=args.model)
    config = SessionConfig(
        model=args.model,
        voice=args.voice,
        input_audio=INPUT_FORMAT,
        output_audio=OUTPUT_FORMAT,
        turn_mode=args.turn_mode,
        control_profile="client_forced" if args.cancel_after_chunks else "native_server",
        system_prompt="请用一句简短、自然的中文回答用户。",
    )

    def factory(context, sink, clock):
        return QwenRealtimeAdapter(context, sink, settings=settings, clock=clock)

    result = asyncio.run(
        run_connection_probe(
            factory,
            audio_path=args.audio,
            output=args.output,
            config=config,
            secrets=(secret,),
            cancel_after_chunks=args.cancel_after_chunks,
            provenance={"source": args.audio_source},
        )
    )
    print(
        json.dumps(
            {
                key: result[key]
                for key in (
                    "success",
                    "run_id",
                    "model",
                    "voice",
                    "turn_mode",
                    "received_audio_chunks",
                    "received_audio_duration_ms",
                    "response_statuses",
                    "cancel_confirmations",
                    "failure",
                )
            },
            ensure_ascii=False,
        )
    )
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
