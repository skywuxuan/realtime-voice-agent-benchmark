import asyncio
import base64
import hashlib
import json

import pytest
from test_qwen import FAKE_SECRET, FakeSocket, factory_for, session_config

from adapters.base import ToolDefinition
from adapters.qwen.config import QwenSettings, session_update
from agent.evaluate import evaluate
from agent.run import load_inputs
from agent.runtime import public_config, run_agent_case
from benchmark.audio import AudioFormat
from benchmark.config import LatencyProfile
from benchmark.contracts import canonical_json
from dataset.cockpit import (
    audit_sources,
    compile_cockpit_dataset,
    load_cases,
    load_protocol,
    select_cases,
)
from dataset.schema import TTSProfile
from events.replay import read_recording
from renderers.base import RenderedText, TTSRenderer
from tools.catalog import ProtocolToolServer, ToolCatalog, ToolCatalogReference
from tools.scenarios import AgentScenario


class FixtureRenderer(TTSRenderer):
    def __init__(self):
        self.calls = []

    def fingerprint(self):
        return {"provider": "fixture", "renderer_version": "fixture-1"}

    def synthesize(self, text):
        self.calls.append(text)
        return RenderedText(
            b"\x20\x00" * 1600,
            AudioFormat(sample_rate_hz=16000),
            {"request_id": f"fixture_{len(self.calls)}"},
        )


def write_sources(tmp_path):
    protocol = tmp_path / "functions.jsonl"
    functions = [
        {
            "name": "setDrivingMode",
            "description": "设置驾驶模式",
            "input_param": {
                "mode": {
                    "description": "模式",
                    "define": {
                        "type": "string",
                        "choice": ["DRIVING_SPORT", "DRIVING_ECO"],
                        "default": "",
                    },
                }
            },
            "skill": "CarControl",
            "classify": "drivingMode",
        },
        {
            "name": "setVolume",
            "description": "设置音量",
            "input_param": {
                "volume": {
                    "description": "音量",
                    "define": {"type": "integer", "choice": [], "default": ""},
                }
            },
            "skill": "CarControl",
            "classify": "volume",
        },
    ]
    protocol.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in functions) + "\n",
        encoding="utf-8",
    )
    testset = tmp_path / "cases.jsonl"
    cases = [
        {
            "dlg_function": "",
            "dlg_domain": "",
            "case": "切换到运动模式",
            "function_result": {
                "name": "setDrivingMode",
                "param": {"mode": "DRIVING_SPORT"},
            },
        },
        {
            "dlg_function": "",
            "dlg_domain": "",
            "case": "音量调到四十",
            "function_result": {"name": "setVolume", "param": {"volume": 40}},
        },
    ]
    testset.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in cases) + "\n",
        encoding="utf-8",
    )
    return protocol, testset


def test_cockpit_compiler_converts_protocol_and_freezes_selected_lines(tmp_path):
    protocol, testset = write_sources(tmp_path)
    renderer = FixtureRenderer()
    result = compile_cockpit_dataset(
        protocol_path=protocol,
        testset_path=testset,
        case_lines=(2, 1),
        dataset_id="fixture_cockpit",
        profile=TTSProfile(
            profile_id="fixture_16k_v1",
            provider="qwen",
            model="fixture-tts",
            voice="fixture",
        ),
        renderer=renderer,
        asset_root=tmp_path,
        allow_render=True,
    )
    assert renderer.calls == ["音量调到四十", "切换到运动模式"]
    assert result.provider_calls == 2
    catalog = ToolCatalog.model_validate_json(result.catalog_path.read_text())
    definitions = {tool.name: tool for tool in catalog.tools}
    assert definitions["setDrivingMode"].parameters["properties"]["mode"]["enum"] == [
        "DRIVING_SPORT",
        "DRIVING_ECO",
    ]
    assert definitions["setVolume"].parameters["properties"]["volume"]["type"] == "integer"
    scenarios, _ = load_inputs(result.suite_path, asset_root=tmp_path)
    assert [case.world["source_line_number"] for case in scenarios] == [2, 1]
    assert scenarios[0].expected_calls[0] == {
        "tool": "setVolume",
        "arguments": {"volume": 40},
    }
    assert all(case.tool_backend == "protocol_ack_v1" for case in scenarios)


def test_cockpit_range_selection_is_one_based_and_skips_no_tool_rows(tmp_path):
    protocol, testset = write_sources(tmp_path)
    with testset.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "dlg_function": "",
                    "dlg_domain": "",
                    "case": "今天天气不错",
                    "function_result": {"name": "", "param": {}},
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        stream.write(
            json.dumps(
                {
                    "dlg_function": "",
                    "dlg_domain": "",
                    "case": "再切回经济模式",
                    "function_result": {
                        "name": "setDrivingMode",
                        "param": {"mode": "DRIVING_ECO"},
                    },
                },
                ensure_ascii=False,
            )
            + "\n"
        )
    selected = select_cases(load_cases(testset), start_line=2, limit=2)
    assert [row.line_number for row in selected] == [2, 4]
    audit = audit_sources(load_protocol(protocol), load_cases(testset))
    assert audit["counts"] == {
        "total": 4,
        "tool_cases": 3,
        "valid_tool_cases": 3,
        "invalid_tool_cases": 0,
        "no_tool_cases": 1,
    }


def test_protocol_server_is_strict_idempotent_and_deterministic():
    catalog = ToolCatalog(
        catalog_id="fixture_catalog",
        source={"kind": "fixture"},
        tools=(
            ToolDefinition(
                name="setDrivingMode",
                description="设置驾驶模式",
                parameters={
                    "type": "object",
                    "properties": {"mode": {"type": "string", "enum": ["SPORT"]}},
                    "additionalProperties": False,
                },
            ),
        ),
    )
    server = ProtocolToolServer(catalog)
    first = server.execute("setDrivingMode", {"mode": "SPORT"}, call_id="call_1")
    assert first.status == "success" and first.result["acknowledged"]
    assert first.result["content"] == "座舱操作已完成"
    assert server.execute("setDrivingMode", {"mode": "SPORT"}, call_id="call_1") == first
    assert len(server.records) == 1 and server.state() == {}
    assert server.execute("setDrivingMode", {"mode": "ECO"}, call_id="call_2").status == "error"
    assert server.execute("unknown", {}, call_id="call_3").error["kind"] == "permission_denied"


def test_qwen_audio_3_profile_uses_smart_turn_and_audio_voice():
    config = session_config().model_copy(
        update={
            "model": "qwen-audio-3.0-realtime-flash",
            "voice": "longanqian",
            "vad": {},
            "turn_mode": "server_vad",
            "provider_options": {"tool_followup_choice": "none"},
        }
    )
    body = session_update(config, QwenSettings(model=config.model))
    assert body["turn_detection"] == {"type": "smart_turn"}
    assert body["modalities"] == ["audio", "text"]
    config_with_tool = config.model_copy(
        update={
            "tools": (
                ToolDefinition(
                    name="fixture",
                    description="fixture",
                    parameters={"type": "object", "properties": {}},
                ),
            )
        }
    )
    assert session_update(config_with_tool, QwenSettings(model=config.model))["tool_choice"] == "auto"
    assert body["voice"] == "longanqian"
    assert "input_audio_transcription" not in body
    with pytest.raises(ValueError, match="smart_turn"):
        session_update(
            config.model_copy(update={"vad": {"silence_duration_ms": 800}}),
            QwenSettings(model=config.model),
        )


class Audio3CockpitToolSocket(FakeSocket):
    def __init__(self):
        super().__init__(auto_response=False)
        self.initial_response_sent = False

    async def send(self, text):
        data = json.loads(text)
        await super().send(text)
        if data["type"] == "input_audio_buffer.append" and not self.initial_response_sent:
            self.initial_response_sent = True
            self.push(
                {"type": "input_audio_buffer.speech_started", "item_id": "u1", "audio_start_ms": 0}
            )
            self.push(
                {"type": "input_audio_buffer.speech_stopped", "item_id": "u1", "audio_end_ms": 100}
            )
            self.push({"type": "input_audio_buffer.committed", "item_id": "u1"})
            self.push(
                {
                    "type": "conversation.item.created",
                    "item": {
                        "id": "u1",
                        "type": "message",
                        "role": "user",
                        "status": "completed",
                        "content": [{"type": "input_audio"}],
                    },
                }
            )
            self.push(
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "item_id": "u1",
                    "transcript": "切换到运动模式",
                }
            )
            self.push(
                {"type": "response.created", "response": {"id": "r1", "status": "in_progress"}}
            )
            self.push(
                {
                    "type": "response.function_call_arguments.done",
                    "response_id": "r1",
                    "name": "setDrivingMode",
                    "arguments": json.dumps({"mode": "DRIVING_SPORT"}),
                    "call_id": "call_1",
                }
            )
            self.push({"type": "response.done", "response": {"id": "r1", "status": "completed"}})
        elif data["type"] == "conversation.item.create":
            self.push(
                {
                    "type": "conversation.item.created",
                    "item": {**data["item"], "status": "completed"},
                }
            )
        elif data["type"] == "response.create":
            self.push(
                {"type": "response.created", "response": {"id": "r2", "status": "in_progress"}}
            )
            self.push(
                {
                    "type": "response.audio.delta",
                    "response_id": "r2",
                    "delta": base64.b64encode(b"\x01\x00" * 480).decode(),
                }
            )
            self.push({"type": "response.audio.done", "response_id": "r2"})
            self.push(
                {"type": "response.done", "response": {"id": "r2", "status": "completed"}}
            )


def test_catalog_agent_recording_replays_from_sealed_catalog(
    tmp_path, scenario_data, context, monkeypatch
):
    monkeypatch.setenv("DASHSCOPE_API_KEY", FAKE_SECRET)
    catalog = ToolCatalog(
        catalog_id="sealed_catalog",
        source={"kind": "fixture"},
        tools=(
            ToolDefinition(
                name="setDrivingMode",
                description="设置驾驶模式",
                parameters={
                    "type": "object",
                    "properties": {
                        "mode": {"type": "string", "enum": ["DRIVING_SPORT"]}
                    },
                    "additionalProperties": False,
                },
            ),
        ),
    )
    catalog_bytes = (canonical_json(catalog.model_dump(mode="json")) + "\n").encode()
    reference = ToolCatalogReference(
        catalog_id=catalog.catalog_id,
        path="datasets/catalog.json",
        sha256=hashlib.sha256(catalog_bytes).hexdigest(),
    )
    scenario = AgentScenario(
        scenario_id=context.scenario_id,
        scenario_version=1,
        user_turns=("切换到运动模式",),
        tool_backend="protocol_ack_v1",
        tool_catalog=reference,
        expected_calls=(
            {"tool": "setDrivingMode", "arguments": {"mode": "DRIVING_SPORT"}},
        ),
        audio_assets={"audio": scenario_data["audio"]["assets"]["question"]},
        turn_assets=("audio",),
        allow_retries=False,
    )
    config = session_config(mode="server_vad", voice="longanqian").model_copy(
        update={
            "model": "qwen-audio-3.0-realtime-flash",
            "provider_options": {"tool_followup_choice": "none"},
        }
    )
    public = public_config(scenario, config, catalog).model_dump_json()
    assert "setDrivingMode" in public and "DRIVING_SPORT" in public
    root = tmp_path / "cockpit-agent"
    socket = Audio3CockpitToolSocket()
    trial = asyncio.run(
        run_agent_case(
            factory_for(socket, model=config.model),
            scenario=scenario,
            source_wavs={"input.wav": (tmp_path / "input.wav").read_bytes()},
            output=root,
            context=context,
            config=config,
            profile=LatencyProfile(
                max_send_lateness_ms=100,
                max_send_duration_ms=100,
                max_playback_lateness_ms=100,
            ),
            secrets=(FAKE_SECRET,),
            tool_catalog=catalog,
        )
    )
    assert trial["status"] == "completed"
    result = evaluate(root)
    assert result["task_completion"] and result["argument_accuracy"] == 1
    assert evaluate(root) == result
    recording = read_recording(root)
    assert any(event.event == "tool_result_sent" for event in recording.events)
    response_creates = [event for event in socket.sent if event["type"] == "response.create"]
    assert len(response_creates) == 1
    assert response_creates[0]["response"]["tool_choice"] == "none"
    session_updates = [event for event in socket.sent if event["type"] == "session.update"]
    assert len(session_updates) == 1
    assert session_updates[0]["session"]["turn_detection"] == {"type": "smart_turn"}
    assert trial["backend"]["audio3_response_policy"] == "server_smart_turn"
