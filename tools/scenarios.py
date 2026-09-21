"""Agent scenario contracts kept separate from realtime audio scenarios."""

from typing import Any, Literal

from pydantic import Field, model_validator

from benchmark.contracts import Contract, Identifier, Sha256, content_hash
from scenarios.schema import AudioAsset
from tools.catalog import ToolCatalogReference


class AgentTurnTrigger(Contract):
    type: Literal["after_response", "after_tool_start"] = "after_response"
    tool: Identifier | None = None
    occurrence: int = Field(default=1, gt=0)
    delay_ms: int = Field(default=0, ge=0, le=30000)

    @model_validator(mode="after")
    def coherent_trigger(self):
        if (self.type == "after_tool_start") != (self.tool is not None):
            raise ValueError("tool trigger requires tool name")
        return self


class ToolDelay(Contract):
    tool: Identifier
    invocation: int = Field(gt=0)
    delay_ms: int = Field(ge=0, le=30000)


class AgentScenario(Contract):
    schema_version: Literal["0.1"] = "0.1"
    scenario_id: Identifier
    scenario_version: int = Field(gt=0)
    model: str = "fixture"
    world: dict[str, Any] = Field(default_factory=dict)
    user_turns: tuple[str, ...] = Field(min_length=1)
    tools_enabled: tuple[Identifier, ...] = ()
    tool_backend: Literal["mock_v1", "protocol_ack_v1"] = "mock_v1"
    tool_catalog: ToolCatalogReference | None = None
    initial_state: dict[str, Any] = Field(default_factory=dict)
    expected_calls: tuple[dict[str, Any], ...] = ()
    expected_final_state: dict[str, Any] = Field(default_factory=dict)
    forbidden_calls: tuple[dict[str, Any], ...] = ()
    failure_schedule: tuple[dict[str, Any], ...] = ()
    tags: tuple[str, ...] = ()
    audio_assets: dict[str, AudioAsset] = Field(default_factory=dict)
    turn_assets: tuple[str, ...] = ()
    allow_retries: bool = True
    max_tool_calls: int = Field(default=12, gt=0, le=100)
    response_timeout_s: float = Field(default=30, gt=0, le=300)
    drain_timeout_s: float = Field(default=60, gt=0, le=300)
    system_prompt: str = "请用中文简短回答；只有工具实际完成后才能声称已执行。"
    turn_triggers: tuple[AgentTurnTrigger, ...] = ()
    tool_delays: tuple[ToolDelay, ...] = ()
    argument_comparison: Literal["exact", "typed_iso8601"] = "exact"
    input_chunk_ms: int = Field(default=20, strict=True, gt=0, le=1000)

    @model_validator(mode="after")
    def coherent_plan(self):
        if (self.tool_backend == "protocol_ack_v1") != (self.tool_catalog is not None):
            raise ValueError("protocol_ack_v1 requires exactly one external tool catalog")
        if self.tool_backend == "protocol_ack_v1" and (
            self.initial_state or self.failure_schedule or self.argument_comparison != "exact"
        ):
            raise ValueError("protocol catalog cases require stateless exact evaluation")
        if self.turn_triggers and len(self.turn_triggers) != len(self.user_turns) - 1:
            raise ValueError("one trigger required per subsequent user turn")
        slots = [(d.tool, d.invocation) for d in self.tool_delays]
        if len(slots) != len(set(slots)):
            raise ValueError("duplicate tool delay slot")
        if any(d.tool not in self.tools_enabled for d in self.tool_delays):
            raise ValueError("delay refers to a disabled tool")
        seen = set()

        def refs(value):
            if isinstance(value, dict):
                if set(value) == {"$result"}:
                    ref = value["$result"]
                    if not isinstance(ref, dict) or set(ref) != {"step", "path"}:
                        raise ValueError("result reference requires step and path")
                    if (
                        not isinstance(ref["path"], list)
                        or not ref["path"]
                        or any(
                            type(key) not in {str, int} or (type(key) is int and key < 0)
                            for key in ref["path"]
                        )
                    ):
                        raise ValueError("invalid result path")
                    return {ref["step"]}
                return set().union(*(refs(item) for item in value.values()))
            if isinstance(value, list):
                return set().union(*(refs(item) for item in value))
            return set()

        for index, expected in enumerate(self.expected_calls):
            step = expected.get("step_id", f"step_{index}")
            if not isinstance(step, str) or not step or step in seen:
                raise ValueError("expected step IDs must be unique")
            if (
                not (set(expected.get("depends_on", [])) | refs(expected.get("arguments", {})))
                <= seen
            ):
                raise ValueError("dependencies must reference earlier expected steps")
            seen.add(step)
        return self

    @property
    def sha256(self) -> Sha256:
        return content_hash(self.model_dump(mode="json"))
