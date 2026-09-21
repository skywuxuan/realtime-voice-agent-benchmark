"""Doubao adapter boundary; live protocol is intentionally deferred until official verification."""

from adapters.deferred import DeferredProtocolAdapter


class DoubaoRealtimeAdapter(DeferredProtocolAdapter):
    def __init__(self, context, sink, *, clock):
        super().__init__(context, sink, clock, provider="doubao-realtime")
