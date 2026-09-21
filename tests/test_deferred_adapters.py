import asyncio

import pytest

from adapters.base import UnsupportedCapability
from adapters.deferred import DeferredProtocolAdapter
from events.clock import SystemClock
from events.schema import RecordingContext


def test_unverified_provider_refuses_live_protocol_without_claiming_capabilities():
    context = RecordingContext(run_id="r", scenario_id="s", attempt_id="a", session_id="i")
    adapter = DeferredProtocolAdapter(context, object(), SystemClock(), provider="step-realtime")
    assert adapter.capabilities().get("audio_input").status == "unknown"
    with pytest.raises(UnsupportedCapability):
        asyncio.run(adapter.connect())
