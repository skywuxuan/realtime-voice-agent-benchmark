"""Qwen3-Omni local adapter boundary.

The current official local interfaces are batch/turn-streaming candidates, not a
verified drop-in commercial realtime protocol. This adapter therefore refuses live
use until a backend profile explicitly supplies the verified process/API contract.
"""

from adapters.deferred import DeferredProtocolAdapter


class QwenOmniAdapter(DeferredProtocolAdapter):
    def __init__(self, context, sink, *, clock):
        super().__init__(context, sink, clock, provider="qwen3-omni-local")
