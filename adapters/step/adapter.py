"""Step adapter boundary; live protocol is intentionally deferred until official verification."""

from adapters.deferred import DeferredProtocolAdapter


class StepRealtimeAdapter(DeferredProtocolAdapter):
    def __init__(self, context, sink, *, clock):
        super().__init__(context, sink, clock, provider="step-realtime")
