"""Protocol profile checked against the official SDK and live session responses."""

from typing import Annotated, Literal

from pydantic import Field

from adapters.base import SessionConfig
from benchmark.audio import AudioFormat
from benchmark.contracts import Contract, Identifier, PositiveInt

SDK_SOURCE = (
    "https://github.com/dashscope/dashscope-sdk-python/blob/"
    "b0b4469e13dd1b0c1d842f99fbfc6a300c5dc760/dashscope/audio/qwen_omni/omni_realtime.py"
)
ADAPTER_VERSION = "0.4.1"
INPUT_FORMAT = AudioFormat(sample_rate_hz=16000)
OUTPUT_FORMAT = AudioFormat(sample_rate_hz=24000)
AUDIO_3_REALTIME_MODELS = {
    "qwen-audio-3.0-realtime-flash",
    "qwen-audio-3.0-realtime-plus",
}


class QwenSettings(Contract):
    model: Identifier = "qwen3.5-omni-flash-realtime"
    # Only the verified Beijing endpoint is enabled in this profile.
    endpoint: Literal["wss://dashscope.aliyuncs.com/api-ws/v1/realtime"] = (
        "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
    )
    transcription_model: Identifier | None = "qwen3-asr-flash-realtime"
    request_timeout_s: Annotated[float, Field(gt=0, le=120)] = 15.0
    close_timeout_s: Annotated[float, Field(gt=0, le=30)] = 5.0
    queue_capacity: PositiveInt = 1024
    max_message_bytes: PositiveInt = 8 * 1024 * 1024


def session_update(config: SessionConfig, settings: QwenSettings) -> dict:
    if config.model != settings.model:
        raise ValueError("configured model differs from the connected Qwen model")
    if config.input_audio != INPUT_FORMAT or config.output_audio != OUTPUT_FORMAT:
        raise ValueError("Qwen profile requires 16 kHz input / 24 kHz output, mono PCM16")
    audio_3 = settings.model in AUDIO_3_REALTIME_MODELS
    if config.sampling:
        raise ValueError("sampling options are not verified in the Qwen profile")
    allowed_provider_options = {"tool_followup_choice"} if audio_3 else set()
    if set(config.provider_options) - allowed_provider_options:
        raise ValueError("unrecognized Qwen provider option")
    if config.provider_options.get("tool_followup_choice", "auto") not in {"auto", "none"}:
        raise ValueError("tool_followup_choice must be auto or none")
    allowed_vad = {
        "threshold",
        "prefix_padding_ms",
        "silence_duration_ms",
        "create_response",
        "interrupt_response",
    }
    if set(config.vad) - allowed_vad:
        raise ValueError("unrecognized Qwen VAD option")
    if audio_3:
        if config.vad:
            raise ValueError("Qwen Audio 3.0 smart_turn profile does not accept server VAD tuning")
        turn_detection = {"type": "smart_turn"}
    else:
        turn_detection = {
            "type": "server_vad",
            "threshold": 0.5,
            "prefix_padding_ms": 300,
            "silence_duration_ms": 800,
            "create_response": True,
            "interrupt_response": True,
            **config.vad,
        }
        if not isinstance(
            turn_detection["threshold"], (int, float)
        ) or not 0 <= turn_detection["threshold"] <= 1:
            raise ValueError("VAD threshold must be between zero and one")
        for name in ("prefix_padding_ms", "silence_duration_ms"):
            if type(turn_detection[name]) is not int or turn_detection[name] < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        for name in ("create_response", "interrupt_response"):
            if type(turn_detection[name]) is not bool:
                raise ValueError(f"{name} must be a boolean")
    body = {
        "modalities": ["audio", "text"],
        "voice": config.voice or ("longanqian" if audio_3 else "Tina"),
        "input_audio_format": "pcm16",
        "output_audio_format": "pcm16",
        "instructions": config.system_prompt,
        "turn_detection": turn_detection if config.turn_mode == "server_vad" else None,
    }
    if settings.transcription_model and not audio_3:
        body["input_audio_transcription"] = {"model": settings.transcription_model}
    if config.temperature is not None:
        body["temperature"] = config.temperature
    if config.tools:
        body["tools"] = [
            {"type": "function", "function": tool.model_dump(mode="json")} for tool in config.tools
        ]
        if audio_3:
            body["tool_choice"] = "auto"
    return body
