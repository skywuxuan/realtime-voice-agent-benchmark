"""Verified StepAudio 3 Realtime endpoint and session profile."""

from typing import Literal

from pydantic import Field

from adapters.base import SessionConfig
from benchmark.audio import AudioFormat
from benchmark.contracts import Contract, PositiveInt

ADAPTER_VERSION = "stepaudio-3-realtime-0.1.0"
PROTOCOL_DOC = "https://platform.stepfun.com/docs/zh/api-reference/realtime/chat"
DEVELOPER_DOC = "https://platform.stepfun.com/docs/zh/guides/developer/realtime"
ENDPOINT = "wss://api.stepfun.com/v1/realtime"
MODEL = "stepaudio-3-realtime-preview"
INPUT_FORMAT = AudioFormat(sample_rate_hz=24000)
OUTPUT_FORMAT = AudioFormat(sample_rate_hz=24000)


class StepSettings(Contract):
    model: Literal[MODEL] = MODEL
    endpoint: Literal[ENDPOINT] = ENDPOINT
    request_timeout_s: float = Field(default=20, gt=0, le=120)
    close_timeout_s: float = Field(default=5, gt=0, le=30)
    queue_capacity: PositiveInt = 1024
    max_message_bytes: PositiveInt = 8 * 1024 * 1024


def session_update(config: SessionConfig, settings: StepSettings) -> dict:
    if config.model != settings.model:
        raise ValueError("StepAudio adapter only supports stepaudio-3-realtime-preview")
    if config.input_audio != INPUT_FORMAT or config.output_audio != OUTPUT_FORMAT:
        raise ValueError("StepAudio 3 Realtime requires 24 kHz input and output mono PCM16")
    if config.sampling:
        raise ValueError("sampling options are not verified in the StepAudio profile")
    if config.vad:
        raise ValueError("StepAudio server VAD does not accept unverified VAD tuning")
    body = {
        "modalities": ["audio", "text"],
        "input_audio_format": "pcm16",
        "output_audio_format": "pcm16",
        "instructions": config.system_prompt,
        "turn_detection": {"type": "server_vad"} if config.turn_mode == "server_vad" else None,
    }
    if config.voice:
        body["voice"] = config.voice
    if config.temperature is not None:
        body["temperature"] = config.temperature
    if config.tools:
        body["tools"] = [
            {"type": "function", "function": tool.model_dump(mode="json")}
            for tool in config.tools
        ]
        body["tool_choice"] = "auto"
    return body
