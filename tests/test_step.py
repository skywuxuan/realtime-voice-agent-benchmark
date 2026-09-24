import asyncio

from adapters.base import CapabilityManifest, SessionConfig, ToolDefinition
from adapters.step import StepRealtimeAdapter, StepSettings
from adapters.step.config import ENDPOINT, INPUT_FORMAT, MODEL, OUTPUT_FORMAT, session_update
from adapters.step.protocol import StepEventMapper


def step_config(mode="server_vad"):
    return SessionConfig(
        model=MODEL,
        turn_mode=mode,
        input_audio=INPUT_FORMAT,
        output_audio=OUTPUT_FORMAT,
        system_prompt="你是智能座舱助手。",
    )


def test_step_profile_uses_verified_endpoint_and_24khz_session():
    settings = StepSettings()
    assert settings.endpoint == ENDPOINT
    body = session_update(
        step_config().model_copy(
            update={
                "tools": (
                    ToolDefinition(
                        name="setVolume",
                        description="设置音量",
                        parameters={"type": "object", "properties": {}},
                    ),
                )
            }
        ),
        settings,
    )
    assert body["turn_detection"] == {"type": "server_vad"}
    assert body["input_audio_format"] == "pcm16"
    assert body["output_audio_format"] == "pcm16"
    assert body["tools"][0]["function"]["name"] == "setVolume"


def test_step_mapper_fills_single_response_ids_and_normalizes_cancel(context, clock):
    mapper = StepEventMapper(context, object(), lambda: CapabilityManifest())

    async def run():
        created = await mapper.normalize(
            {"type": "response.created", "response": {"id": "step-r1"}},
            clock.now(),
            "created",
        )
        assert created[0].event == "assistant_response_start"
        call = await mapper.normalize(
            {
                "type": "response.function_call_arguments.done",
                "call_id": "call-1",
                "name": "setVolume",
                "arguments": '{"level": 10}',
            },
            clock.now(),
            "call",
        )
        assert call[-1].response_id == "step-r1"
        assert call[-1].payload.arguments == {"level": 10}
        cancelled = await mapper.normalize(
            {"type": "response.cancelled"}, clock.now(), "cancelled"
        )
        assert [event.event for event in cancelled] == [
            "assistant_response_end",
            "assistant_cancelled",
        ]
        assert cancelled[0].payload.status == "cancelled"

    asyncio.run(run())


def test_step_response_commands_follow_step_dialect(monkeypatch, context):
    monkeypatch.setenv("STEPFUN_API_KEY", "step-test-secret")
    adapter = StepRealtimeAdapter(context, object(), settings=StepSettings())
    assert adapter._response_options(tool_followup=True) == {"modalities": ["audio", "text"]}
    assert adapter._include_tool_output_item_id is False
    assert adapter._allow_empty_manual_turn_detection is True
    assert adapter.tool_dispatch_policy() == "tool_call_end"
    assert adapter.diagnostics()["connection_url"] == (
        "wss://api.stepfun.com/v1/realtime?model=stepaudio-3-realtime-preview"
    )
