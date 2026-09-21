"""Versioned event envelopes with event-specific, validated payloads."""

import uuid
from typing import Annotated, Literal

from pydantic import Field, JsonValue, SerializeAsAny, model_validator

from benchmark.audio import AudioFormat, AudioRef
from benchmark.contracts import (
    Contract,
    Identifier,
    NonNegativeInt,
    PositiveInt,
    RelativePath,
    Sha256,
)
from events.clock import Clock, ClockReading

EventType = Literal[
    "session_start",
    "session_end",
    "session_configured",
    "user_audio_start",
    "user_audio_chunk",
    "user_audio_end",
    "input_region_start",
    "input_region_end",
    "user_turn_commit",
    "user_text_done",
    "vad_start",
    "vad_end",
    "assistant_response_start",
    "assistant_response_end",
    "assistant_audio_start",
    "assistant_audio_chunk",
    "assistant_audio_end",
    "assistant_text_delta",
    "assistant_text_done",
    "interrupt_start",
    "interrupt_detected",
    "interrupt_requested",
    "assistant_cancelled",
    "tool_call_start",
    "tool_call_arguments",
    "tool_call_end",
    "tool_result",
    "tool_execution_start",
    "tool_execution_end",
    "tool_result_sent",
    "scenario_action_start",
    "scenario_action_end",
    "assistant_playback_start",
    "assistant_playback_chunk",
    "assistant_playback_stop",
    "playback_buffer_cleared",
    "audio_chunk_dropped",
    "backchannel_start",
    "backchannel_end",
    "case_end",
    "error",
]


class RecordingContext(Contract):
    run_id: Identifier
    scenario_id: Identifier
    attempt_id: Identifier
    session_id: Identifier


class Timing(Contract):
    basis: Literal[
        "client_receive",
        "client_send",
        "simulator_boundary",
        "virtual_playback",
        "device_playback",
        "inferred",
    ]
    uncertainty_ns: NonNegativeInt | None = None


class Payload(Contract):
    vendor: dict[str, JsonValue] = Field(default_factory=dict)


class SessionStart(Payload):
    vendor_session_id: Identifier | None
    adapter_version: Identifier
    capabilities: dict[str, JsonValue]


class SessionEnd(Payload):
    reason: str
    complete: bool
    last_response_ids: tuple[Identifier, ...] = ()


class SessionConfigured(Payload):
    requested: dict[str, JsonValue]
    effective: dict[str, JsonValue]
    unverified: dict[str, JsonValue]
    evidence_event_ids: tuple[Identifier, ...] = ()


class UserAudioStart(Payload):
    action_id: Identifier
    asset_id: Identifier
    sample_index: NonNegativeInt
    annotation_source: str


class UserAudioEnd(Payload):
    action_id: Identifier
    end_sample: NonNegativeInt
    annotation_source: str


class InputRegionBoundary(Payload):
    action_id: Identifier
    region_id: Identifier
    kind: Literal["pause", "interferer"]
    sample_index: NonNegativeInt


class TurnCommit(Payload):
    phase: Literal["requested", "submitted"]


class UserAudioChunk(Payload):
    audio_ref: AudioRef
    chunk_index: NonNegativeInt
    planned_send_ns: NonNegativeInt
    send_started_ns: NonNegativeInt
    send_completed_ns: NonNegativeInt
    silence: bool

    @model_validator(mode="after")
    def ordered_send(self) -> "UserAudioChunk":
        if self.send_completed_ns < self.send_started_ns:
            raise ValueError("send completion precedes send start")
        return self


class Vad(Payload):
    detector: str
    vendor_item_id: str | None = None
    vendor_audio_offset_ms: Annotated[float, Field(ge=0)] | None = None


class ResponseStart(Payload):
    response_status: str
    trigger_turn_id: Identifier | None
    association_method: Literal["vendor_ids", "inferred_serial_turn", "ambiguous"]


class ResponseEnd(Payload):
    status: Literal["completed", "cancelled", "failed", "unknown"]
    completion_source: str


class AudioStart(Payload):
    first_chunk_event_id: Identifier
    audio_format: AudioFormat


class AudioChunk(Payload):
    audio_ref: AudioRef
    chunk_index: NonNegativeInt
    late_after_cancel: bool = False


class AudioEnd(Payload):
    reason: str
    last_chunk_event_id: Identifier | None
    complete: bool
    completion_source: str


class TextDelta(Payload):
    text: str
    channel: Literal["spoken_transcript", "text_response"]
    delta_index: NonNegativeInt


class TextDone(Payload):
    text: str
    channel: Literal["spoken_transcript", "text_response", "input_transcript"]
    completion_source: str
    partial: bool = False


class Stimulus(Payload):
    action_id: Identifier
    target_response_id: Identifier
    intent_revision: PositiveInt | None = None


class InterruptionDetected(Payload):
    target_response_id: Identifier
    mechanism: str
    evidence_event_ids: Annotated[tuple[Identifier, ...], Field(min_length=1)]
    evidence_level: Literal["confirmed", "inferred"]


class Cancelled(Payload):
    target_response_id: Identifier
    initiator: Literal["server", "client", "unknown"]
    reason: str
    evidence: Annotated[tuple[Identifier, ...], Field(min_length=1)]


class InterruptRequested(Payload):
    target_response_id: Identifier
    command_id: Identifier
    reason: str


class ToolCallStart(Payload):
    name: Identifier
    call_id: Identifier
    response_id: Identifier


class ToolArguments(Payload):
    representation: Literal["delta", "final"]
    text: str
    parsed_arguments: dict[str, JsonValue] | None = None
    parse_status: Literal["partial", "valid", "invalid"]


class ToolCallEnd(Payload):
    name: Identifier
    arguments: dict[str, JsonValue] | None
    valid_json: bool
    completion_source: str

    @model_validator(mode="after")
    def parsed_or_invalid(self) -> "ToolCallEnd":
        if self.valid_json != (self.arguments is not None):
            raise ValueError("arguments must be present exactly when valid_json is true")
        return self


class ToolResult(Payload):
    call_id: Identifier
    status: Literal["success", "error"]
    result: JsonValue = None
    error: dict[str, JsonValue] | None = None
    state_hash: Sha256 | None = None
    execution_id: Identifier

    @model_validator(mode="after")
    def result_status(self) -> "ToolResult":
        if (self.status == "error") != (self.error is not None):
            raise ValueError("error details must match tool result status")
        return self


class ToolExecution(Payload):
    execution_id: Identifier
    state_version: NonNegativeInt
    failure_fixture: str | None = None
    status: str


class ToolResultSent(Payload):
    execution_id: Identifier
    command_id: Identifier


class ActionStatus(Payload):
    action_id: Identifier
    target_response_id: Identifier | None = None
    timed_out: bool = False
    reason: str | None = None


class Playback(Payload):
    sample_offset: NonNegativeInt
    sample_count: NonNegativeInt
    sample_rate_hz: PositiveInt
    stop_reason: str | None = None
    audio_ref: AudioRef | None = None
    planned_end_ns: NonNegativeInt | None = None
    wake_lateness_ns: NonNegativeInt | None = None


class BufferCleared(Payload):
    initiator: str
    policy: str
    dropped_sample_count: NonNegativeInt
    trigger_event_id: Identifier


class AudioDropped(Payload):
    audio_ref: AudioRef
    reason: str
    chunk_event_id: Identifier


class CaseEnd(Payload):
    status: Literal["completed", "model_failed", "infra_failed", "invalid", "unsupported"]
    reason: str


class Error(Payload):
    category: str
    code: str
    message_redacted: str
    fatal: bool
    scope: str
    retryable: bool


PAYLOAD_TYPES: dict[str, type[Payload]] = {
    "session_start": SessionStart,
    "session_end": SessionEnd,
    "session_configured": SessionConfigured,
    "user_audio_start": UserAudioStart,
    "user_audio_end": UserAudioEnd,
    "input_region_start": InputRegionBoundary,
    "input_region_end": InputRegionBoundary,
    "user_turn_commit": TurnCommit,
    "user_audio_chunk": UserAudioChunk,
    "vad_start": Vad,
    "vad_end": Vad,
    "assistant_response_start": ResponseStart,
    "assistant_response_end": ResponseEnd,
    "assistant_audio_start": AudioStart,
    "assistant_audio_chunk": AudioChunk,
    "assistant_audio_end": AudioEnd,
    "assistant_text_delta": TextDelta,
    "assistant_text_done": TextDone,
    "user_text_done": TextDone,
    "interrupt_start": Stimulus,
    "backchannel_start": Stimulus,
    "backchannel_end": Stimulus,
    "interrupt_detected": InterruptionDetected,
    "assistant_cancelled": Cancelled,
    "interrupt_requested": InterruptRequested,
    "tool_call_start": ToolCallStart,
    "tool_call_arguments": ToolArguments,
    "tool_call_end": ToolCallEnd,
    "tool_result": ToolResult,
    "tool_execution_start": ToolExecution,
    "tool_execution_end": ToolExecution,
    "tool_result_sent": ToolResultSent,
    "scenario_action_start": ActionStatus,
    "scenario_action_end": ActionStatus,
    "assistant_playback_start": Playback,
    "assistant_playback_chunk": Playback,
    "assistant_playback_stop": Playback,
    "playback_buffer_cleared": BufferCleared,
    "audio_chunk_dropped": AudioDropped,
    "case_end": CaseEnd,
    "error": Error,
}


class EventDraft(RecordingContext, ClockReading):
    schema_version: Literal["0.1"] = "0.1"
    event_id: Identifier = Field(default_factory=lambda: f"ev_{uuid.uuid4().hex}")
    source: Literal["user", "assistant", "system", "tool"]
    producer: Identifier
    event: EventType
    turn_id: Identifier | None = None
    response_id: Identifier | None = None
    item_id: Identifier | None = None
    call_id: Identifier | None = None
    stream_id: Identifier | None = None
    causal_event_id: Identifier | None = None
    raw_event_ref: Identifier | None = None
    timing: Timing
    payload: SerializeAsAny[Payload]

    @model_validator(mode="before")
    @classmethod
    def typed_payload(cls, data: object) -> object:
        if isinstance(data, dict) and data.get("event") in PAYLOAD_TYPES:
            data = dict(data)
            payload = data.get("payload", {})
            if isinstance(payload, Payload):
                payload = payload.model_dump(mode="python")
            data["payload"] = PAYLOAD_TYPES[data["event"]].model_validate(payload)
        return data

    @model_validator(mode="after")
    def correlation(self) -> "EventDraft":
        if self.event.startswith("assistant_") or self.event in {
            "playback_buffer_cleared",
            "audio_chunk_dropped",
            "interrupt_start",
            "interrupt_detected",
            "interrupt_requested",
            "backchannel_start",
            "backchannel_end",
        }:
            if self.response_id is None:
                raise ValueError(f"{self.event} requires response_id")
        if self.event.startswith("tool_") and self.call_id is None:
            raise ValueError(f"{self.event} requires call_id")
        for name in ("call_id", "response_id"):
            nested = getattr(self.payload, name, None)
            if nested is not None and nested != getattr(self, name):
                raise ValueError(f"payload {name} differs from envelope")
        target = getattr(self.payload, "target_response_id", None)
        if target is not None and target != self.response_id:
            raise ValueError("target_response_id differs from envelope response_id")
        if self.event in {"user_audio_start", "user_audio_end", "user_audio_chunk"}:
            if self.turn_id is None or self.stream_id is None:
                raise ValueError("user audio requires turn_id and stream_id")
        if self.event == "assistant_audio_chunk" and self.stream_id is None:
            raise ValueError("assistant audio requires stream_id")
        return self


class NormalizedEvent(EventDraft):
    seq: PositiveInt
    recorded_monotonic_ns: NonNegativeInt

    @model_validator(mode="after")
    def not_recorded_before_observation(self) -> "NormalizedEvent":
        if self.recorded_monotonic_ns < self.timestamp_monotonic_ns:
            raise ValueError("record time cannot precede observation time")
        return self


class BlobRef(Contract):
    path: RelativePath
    sha256: Sha256
    byte_length: NonNegativeInt


class RawEvent(RecordingContext, ClockReading):
    schema_version: Literal["0.1"] = "0.1"
    raw_event_id: Identifier = Field(default_factory=lambda: f"raw_{uuid.uuid4().hex}")
    direction: Literal["sent", "received"]
    transport: str
    vendor_event_type: str | None = None
    body: dict[str, JsonValue] | None = None
    body_ref: BlobRef | None = None
    redacted_fields: tuple[str, ...] = ()

    @model_validator(mode="after")
    def one_body(self) -> "RawEvent":
        if (self.body is None) == (self.body_ref is None):
            raise ValueError("exactly one of body and body_ref is required")
        return self


def new_event(context: RecordingContext, clock: Clock, **fields: object) -> EventDraft:
    return EventDraft(**context.model_dump(), **clock.now().model_dump(), **fields)
