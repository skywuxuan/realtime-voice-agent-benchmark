"""Protocol profile checked against the official SDK and live session responses."""

from typing import Annotated, Literal

from pydantic import Field

from adapters.base import SessionConfig
from benchmark.audio import AudioFormat
from benchmark.contracts import Contract, PositiveInt

SDK_SOURCE = (
    "https://github.com/aliyun/alibabacloud-bailian-speech-demo/blob/"
    "1082942345c555429ee61c04b8c585f12e06dab2/samples/conversation/"
    "fun-audiochat-realtime/fun_realtime/client.py"
)
ADAPTER_VERSION = "0.5.0"
INPUT_FORMAT = AudioFormat(sample_rate_hz=16000)
OUTPUT_FORMAT = AudioFormat(sample_rate_hz=24000)
class QwenSettings(Contract):
    model: Literal[
        "qwen-audio-3.0-realtime-flash", "qwen-audio-3.0-realtime-plus"
    ] = "qwen-audio-3.0-realtime-flash"
    # Only the verified Beijing endpoint is enabled in this profile.
    endpoint: Literal["wss://dashscope.aliyuncs.com/api-ws/v1/realtime"] = (
        "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
    )
    request_timeout_s: Annotated[float, Field(gt=0, le=120)] = 15.0
    close_timeout_s: Annotated[float, Field(gt=0, le=30)] = 5.0
    queue_capacity: PositiveInt = 1024
    max_message_bytes: PositiveInt = 8 * 1024 * 1024


def session_update(config: SessionConfig, settings: QwenSettings) -> dict:
    if config.model != settings.model:
        raise ValueError("configured model differs from the connected Qwen model")
    if config.input_audio != INPUT_FORMAT or config.output_audio != OUTPUT_FORMAT:
        raise ValueError("Qwen profile requires 16 kHz input / 24 kHz output, mono PCM16")
    if config.sampling:
        raise ValueError("sampling options are not verified in the Qwen profile")
    allowed_provider_options = {"tool_followup_choice"}
    if set(config.provider_options) - allowed_provider_options:
        raise ValueError("unrecognized Qwen provider option")
    if config.provider_options.get("tool_followup_choice", "auto") not in {"auto", "none"}:
        raise ValueError("tool_followup_choice must be auto or none")
    if config.vad:
        raise ValueError("Qwen Audio 3.0 smart_turn profile does not accept VAD tuning")
    turn_detection = {"type": "smart_turn"}
    body = {
        "modalities": ["audio", "text"],
        "voice": config.voice or "longanqian",
        "input_audio_format": "pcm16",
        "output_audio_format": "pcm16",
        "instructions": config.system_prompt,
        "turn_detection": turn_detection if config.turn_mode == "server_vad" else None,
    }
    if config.temperature is not None:
        body["temperature"] = config.temperature
    if config.tools:
        body["tools"] = [
            {"type": "function", "function": tool.model_dump(mode="json")} for tool in config.tools
        ]
        body["tool_choice"] = "auto"
    return body
