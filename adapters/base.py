"""Async adapter contract and common lifecycle guards, without vendor imports."""

import asyncio
from abc import ABC, abstractmethod
from typing import Literal

from pydantic import Field, JsonValue, model_validator

from benchmark.audio import AudioFormat, AudioFrame
from benchmark.contracts import Contract, Identifier, NonNegativeInt, PositiveInt
from events.clock import ClockReading
from events.schema import EventDraft, ToolResult

CapabilityName = Literal[
    "audio_input",
    "audio_output",
    "streaming_input",
    "streaming_output",
    "server_vad",
    "native_full_duplex",
    "native_interrupt",
    "client_cancel",
    "tool_calling",
    "tool_result_injection",
    "context_truncation",
    "playback_ack",
]


class Capability(Contract):
    status: Literal["supported", "unsupported", "unknown"] = "unknown"
    verification: Literal["docs", "experiment", "unverified"] = "unverified"
    evidence: tuple[str, ...] = ()

    @model_validator(mode="after")
    def evidence_required(self) -> "Capability":
        if self.status != "unknown" and (not self.evidence or self.verification == "unverified"):
            raise ValueError("known capabilities need evidence and a verification level")
        return self


class CapabilityManifest(Contract):
    features: dict[CapabilityName, Capability] = Field(default_factory=dict)

    def get(self, name: CapabilityName) -> Capability:
        return self.features.get(name, Capability())

    def require(self, *names: CapabilityName) -> None:
        missing = {
            name: self.get(name).status for name in names if self.get(name).status != "supported"
        }
        if missing:
            raise UnsupportedCapability(f"required capabilities are not confirmed: {missing}")


class SessionOptions(Contract):
    system_prompt: str = ""
    turn_mode: Literal["manual", "server_vad"] = "server_vad"
    control_profile: Literal["native_server", "client_vad_flush", "client_forced"] = "native_server"


class ToolDefinition(Contract):
    name: Identifier
    description: str
    parameters: dict[str, JsonValue]


class SessionConfig(SessionOptions):
    model: Identifier
    input_audio: AudioFormat
    output_audio: AudioFormat
    voice: str | None = None
    temperature: float | None = None
    vad: dict[str, JsonValue] = Field(default_factory=dict)
    sampling: dict[str, JsonValue] = Field(default_factory=dict)
    provider_options: dict[str, JsonValue] = Field(default_factory=dict)
    tools: tuple[ToolDefinition, ...] = ()


class EffectiveConfig(Contract):
    requested: dict[str, JsonValue]
    effective: dict[str, JsonValue]
    unverified: dict[str, JsonValue]


class SessionInfo(Contract):
    session_id: Identifier
    vendor_session_id: Identifier | None
    adapter_version: Identifier


class SendReceipt(Contract):
    stream_id: Identifier
    chunk_index: NonNegativeInt
    byte_count: PositiveInt
    started: ClockReading
    completed: ClockReading

    @model_validator(mode="after")
    def consistent_time(self) -> "SendReceipt":
        if self.started.clock_id != self.completed.clock_id:
            raise ValueError("send receipt must use one clock domain")
        if self.completed.timestamp_monotonic_ns < self.started.timestamp_monotonic_ns:
            raise ValueError("send receipt completion precedes start")
        return self


class InterruptRequest(Contract):
    target_response_id: Identifier
    reason: str
    played_sample_count: NonNegativeInt | None = None


class CommandReceipt(Contract):
    command_id: Identifier
    submitted: ClockReading


class AdapterStateError(RuntimeError):
    pass


class UnsupportedCapability(RuntimeError):
    pass


class RealtimeModelAdapter(ABC):
    """Implement transport hooks only. Events keep observation time, without recorder seq.

    The runner owns timeouts for connect/configure/send/receive. Close is separately
    bounded, idempotent and does not imply a vendor cancellation acknowledgement.
    """

    def __init__(self, *, close_timeout_s: float = 5.0) -> None:
        if close_timeout_s <= 0:
            raise ValueError("close timeout must be positive")
        self.state = "created"
        self.config: SessionConfig | None = None
        self.close_timeout_s = close_timeout_s
        self._receiving = False
        self._committed_turns: set[str] = set()
        self._close_task: asyncio.Task | None = None

    def _require_state(self, *states: str) -> None:
        if self.state not in states:
            raise AdapterStateError(f"operation is not valid in state {self.state}")

    async def connect(self) -> SessionInfo:
        self._require_state("created")
        self.state = "connecting"
        try:
            result = await self._connect()
        except BaseException:
            if self.state not in {"draining", "closed"}:
                self.state = "failed"
            raise
        if self.state != "connecting":
            raise AdapterStateError("adapter closed during connect")
        self.state = "connected"
        return result

    async def configure(self, config: SessionConfig) -> EffectiveConfig:
        self._require_state("connected")
        if config.turn_mode == "server_vad":
            self.capabilities().require("server_vad")
        self.state = "configuring"
        try:
            result = await self._configure(config)
        except BaseException:
            if self.state not in {"draining", "closed"}:
                self.state = "failed"
            raise
        if self.state != "configuring":
            raise AdapterStateError("adapter closed during configure")
        self.config = config
        self.state = "configured"
        return result

    async def send_audio(self, frame: AudioFrame) -> SendReceipt:
        self._require_state("configured", "running")
        if self.config is None or frame.format != self.config.input_audio:
            raise ValueError("input PCM format must match the configured format")
        if frame.turn_id in self._committed_turns:
            raise AdapterStateError("cannot append audio to an already committed turn")
        self.state = "running"
        return await self._send_audio(frame)

    async def commit_turn(self, turn_id: str) -> None:
        self._require_state("configured", "running")
        if self.config is None or self.config.turn_mode != "manual":
            raise AdapterStateError("explicit commit is only valid in manual turn mode")
        if not turn_id or turn_id in self._committed_turns:
            raise AdapterStateError("turn ID is empty or already committed")
        # Reserve before awaiting; uncertain transport failures must not cause duplicate commits.
        self._committed_turns.add(turn_id)
        await self._commit_turn(turn_id)

    async def receive_event(self) -> EventDraft:
        # Closing the transport doesn't discard already queued normalized events.
        self._require_state(
            "connected", "configuring", "configured", "running", "draining", "closed"
        )
        if self._receiving:
            raise AdapterStateError("receive_event permits only one concurrent consumer")
        self._receiving = True
        try:
            return await self._receive_event()
        finally:
            self._receiving = False

    async def send_tool_result(self, result: ToolResult) -> None:
        self._require_state("configured", "running")
        self.capabilities().require("tool_result_injection")
        await self._send_tool_result(result)

    async def interrupt(self, request: InterruptRequest) -> CommandReceipt:
        self._require_state("configured", "running")
        self.capabilities().require("client_cancel")
        return await self._interrupt(request)

    async def close(self) -> None:
        if self._close_task is None:
            self.state = "draining"
            self._close_task = asyncio.create_task(self._finish_close())
        await asyncio.shield(self._close_task)

    async def _finish_close(self) -> None:
        try:
            async with asyncio.timeout(self.close_timeout_s):
                await self._close()
        finally:
            self.state = "closed"

    @abstractmethod
    def capabilities(self) -> CapabilityManifest: ...

    def diagnostics(self) -> dict:
        """Non-secret backend/protocol metadata, independent of benchmark metrics."""
        return {}

    def tool_dispatch_policy(self) -> Literal["response_end", "tool_call_end"]:
        """When a normalized tool call is safe for the runner to execute."""
        return "response_end"

    @abstractmethod
    async def _connect(self) -> SessionInfo: ...

    @abstractmethod
    async def _configure(self, config: SessionConfig) -> EffectiveConfig: ...

    @abstractmethod
    async def _send_audio(self, frame: AudioFrame) -> SendReceipt: ...

    @abstractmethod
    async def _commit_turn(self, turn_id: str) -> None: ...

    @abstractmethod
    async def _receive_event(self) -> EventDraft: ...

    async def _send_tool_result(self, result: ToolResult) -> None:
        raise UnsupportedCapability("tool result injection is not implemented")

    async def _interrupt(self, request: InterruptRequest) -> CommandReceipt:
        raise UnsupportedCapability("client cancellation is not implemented")

    @abstractmethod
    async def _close(self) -> None: ...
