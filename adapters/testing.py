"""Explicitly synthetic transport for offline contract checks; never benchmark a model with it."""

import asyncio
from collections.abc import Iterable

from adapters.base import (
    Capability,
    CapabilityManifest,
    CommandReceipt,
    EffectiveConfig,
    InterruptRequest,
    RealtimeModelAdapter,
    SendReceipt,
    SessionConfig,
    SessionInfo,
)
from benchmark.audio import AudioFrame
from events.clock import Clock
from events.schema import EventDraft, RecordingContext, ToolResult


class ScriptedAdapter(RealtimeModelAdapter):
    def __init__(self, context: RecordingContext, clock: Clock, events: Iterable[EventDraft] = ()):
        super().__init__()
        self.context = context
        self.clock = clock
        self.queue: asyncio.Queue[EventDraft | None] = asyncio.Queue()
        for event in events:
            self.queue.put_nowait(event)
        self.sent_audio: list[AudioFrame] = []
        self.commits: list[str] = []
        self.tool_results: list[ToolResult] = []
        self.interrupts: list[InterruptRequest] = []
        self.close_calls = 0

    def capabilities(self) -> CapabilityManifest:
        supported = Capability(
            status="supported",
            verification="experiment",
            evidence=("synthetic in-process transport only; not a model capability",),
        )
        return CapabilityManifest(
            features={
                name: supported
                for name in (
                    "audio_input",
                    "audio_output",
                    "streaming_input",
                    "streaming_output",
                    "server_vad",
                    "client_cancel",
                    "tool_result_injection",
                )
            }
        )

    async def _connect(self) -> SessionInfo:
        return SessionInfo(
            session_id=self.context.session_id,
            vendor_session_id=None,
            adapter_version="fixture-0.1",
        )

    async def _configure(self, config: SessionConfig) -> EffectiveConfig:
        data = config.model_dump(mode="json")
        return EffectiveConfig(requested=data, effective=data, unverified={})

    async def _send_audio(self, frame: AudioFrame) -> SendReceipt:
        started = self.clock.now()
        self.sent_audio.append(frame)
        return SendReceipt(
            stream_id=frame.stream_id,
            chunk_index=frame.chunk_index,
            byte_count=len(frame.pcm),
            started=started,
            completed=self.clock.now(),
        )

    async def _commit_turn(self, turn_id: str) -> None:
        self.commits.append(turn_id)

    async def _receive_event(self) -> EventDraft:
        if self.state == "closed" and self.queue.empty():
            raise EOFError("synthetic transport closed")
        event = await self.queue.get()
        if event is None:
            raise EOFError("synthetic transport closed")
        return event

    async def _send_tool_result(self, result: ToolResult) -> None:
        self.tool_results.append(result)

    async def _interrupt(self, request: InterruptRequest) -> CommandReceipt:
        self.interrupts.append(request)
        return CommandReceipt(
            command_id=f"fixture_cancel_{len(self.interrupts)}", submitted=self.clock.now()
        )

    async def _close(self) -> None:
        self.close_calls += 1
        self.queue.put_nowait(None)
