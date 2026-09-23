"""Verified Seed Duplex 3.0 endpoint and session payload."""

from typing import Literal

from pydantic import Field

from adapters.base import SessionConfig
from benchmark.audio import AudioFormat
from benchmark.contracts import Contract, PositiveInt

ADAPTER_VERSION = "doubao-seed-duplex-0.1.0"
PROTOCOL_DOC = "https://www.volcengine.com/docs/6561/2549778"
AUTH_DOC = "https://www.volcengine.com/docs/6561/2534847"
OFFICIAL_DEMO = (
    "https://portal.volccdn.com/obj/volcfe/cloud-universal-doc/"
    "upload_40e78e155a0960f1fa2cdafc098a4254.zip"
)
ENDPOINT = "wss://openspeech.bytedance.com/api/v3/duplex/realtime/dialogue"
RESOURCE_ID = "volc.speech.dialog"
APP_KEY = "PlgvMymc7f3tQnJ6"
INPUT_FORMAT = AudioFormat(sample_rate_hz=16000)
OUTPUT_FORMAT = AudioFormat(sample_rate_hz=24000)


class DoubaoSettings(Contract):
    endpoint: Literal[ENDPOINT] = ENDPOINT
    model_version: Literal["1.2.6.1"] = "1.2.6.1"
    request_timeout_s: float = Field(default=20, gt=0, le=120)
    close_timeout_s: float = Field(default=5, gt=0, le=30)
    input_idle_mute_s: float = Field(default=0.2, gt=0.04, le=2)
    max_message_bytes: PositiveInt = 32 * 1024 * 1024
    queue_capacity: PositiveInt = 512


def session_create(config: SessionConfig, settings: DoubaoSettings) -> dict:
    if config.model != "seed-duplex-3.0":
        raise ValueError("Doubao adapter only supports seed-duplex-3.0")
    if config.input_audio != INPUT_FORMAT or config.output_audio != OUTPUT_FORMAT:
        raise ValueError("Seed Duplex 3.0 requires 16 kHz input and 24 kHz output PCM")
    if config.turn_mode not in {"manual", "server_vad"}:
        raise ValueError("unsupported Seed Duplex turn mode")
    tools = [
        {
            "type": "function",
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        }
        for tool in config.tools
    ]
    return {
        "model": settings.model_version,
        "instructions": config.system_prompt,
        "audio": {
            "input": {"format": {"type": "pcm", "sample_rate": 16000}},
            "output": {
                "format": {"type": "pcm", "sample_rate": 24000},
                "voice": config.voice or "zh_female_vv_jupiter_bigtts",
                "speed": 0,
                "loudness": 0,
            },
        },
        "tools": tools,
    }
