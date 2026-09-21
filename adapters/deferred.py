"""Explicit deferred adapter for vendors whose wire protocol is not yet verified."""

from adapters.base import (
    Capability,
    CapabilityManifest,
    EffectiveConfig,
    RealtimeModelAdapter,
    SendReceipt,
    SessionConfig,
    SessionInfo,
    UnsupportedCapability,
)


class DeferredProtocolAdapter(RealtimeModelAdapter):
    provider: str = "unverified"
    protocol_status: str = "unverified"

    def __init__(self, context, sink, clock, *, provider: str):
        super().__init__()
        self.context, self.sink, self.clock, self.provider = context, sink, clock, provider

    def capabilities(self) -> CapabilityManifest:
        return CapabilityManifest(
            features={
                name: Capability(status="unknown", verification="unverified")
                for name in (
                    "audio_input",
                    "audio_output",
                    "streaming_input",
                    "streaming_output",
                    "server_vad",
                    "native_full_duplex",
                    "client_cancel",
                    "tool_calling",
                )
            }
        )

    async def _connect(self) -> SessionInfo:
        raise UnsupportedCapability(
            f"{self.provider} protocol is not verified; configure a provider adapter before live use"
        )

    async def _configure(self, config: SessionConfig) -> EffectiveConfig:
        raise UnsupportedCapability(f"{self.provider} protocol is not verified")

    async def _send_audio(self, frame) -> SendReceipt:
        raise UnsupportedCapability(f"{self.provider} protocol is not verified")

    async def _commit_turn(self, turn_id: str) -> None:
        raise UnsupportedCapability(f"{self.provider} protocol is not verified")

    async def _receive_event(self):
        raise UnsupportedCapability(f"{self.provider} protocol is not verified")

    async def _close(self) -> None:
        return None
