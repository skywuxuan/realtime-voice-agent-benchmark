"""StepAudio 3 Realtime adapter using the verified StepFun WebSocket dialect."""

from adapters.qwen.adapter import QwenRealtimeAdapter
from adapters.step.config import (
    ADAPTER_VERSION,
    DEVELOPER_DOC,
    INPUT_FORMAT,
    OUTPUT_FORMAT,
    PROTOCOL_DOC,
    StepSettings,
    session_update,
)
from adapters.step.protocol import StepEventMapper


class StepRealtimeAdapter(QwenRealtimeAdapter):
    def __init__(self, context, sink, *, clock=None, settings=None, connector=None):
        super().__init__(
            context,
            sink,
            settings=settings or StepSettings(),
            clock=clock,
            connector=connector,
            api_key_env="STEPFUN_API_KEY",
            session_update_builder=session_update,
            mapper_factory=StepEventMapper,
            input_format=INPUT_FORMAT,
            output_format=OUTPUT_FORMAT,
            provider_name="step",
            protocol_reference=PROTOCOL_DOC,
            strict_echoes=(),
            include_tool_output_item_id=False,
            server_response_policy="server_vad",
            adapter_version=ADAPTER_VERSION,
            allow_empty_manual_turn_detection=True,
            allow_tool_result_before_response_end=True,
        )

    def tool_dispatch_policy(self) -> str:
        return "tool_call_end"

    def _response_options(self, *, tool_followup: bool = False) -> dict:
        return {"modalities": ["audio", "text"]}

    def diagnostics(self) -> dict:
        result = super().diagnostics()
        result.update(
            {
                "adapter_version": ADAPTER_VERSION,
                "protocol_reference": PROTOCOL_DOC,
                "developer_reference": DEVELOPER_DOC,
                "audio_sample_rate_hz": 24000,
            }
        )
        return result
