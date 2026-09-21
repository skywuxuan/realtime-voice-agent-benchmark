import asyncio

import pytest

from adapters.base import (
    AdapterStateError,
    CapabilityManifest,
    InterruptRequest,
    SessionConfig,
    UnsupportedCapability,
)
from adapters.testing import ScriptedAdapter
from benchmark.audio import AudioFormat, AudioFrame


def config(mode="manual"):
    return SessionConfig(
        model="fixture",
        turn_mode=mode,
        input_audio=AudioFormat(sample_rate_hz=16000),
        output_audio=AudioFormat(sample_rate_hz=24000),
    )


def frame():
    return AudioFrame(
        pcm=b"\0\0" * 320,
        format=AudioFormat(sample_rate_hz=16000),
        stream_id="input",
        turn_id="t1",
        chunk_index=0,
        sample_offset=0,
    )


def test_lifecycle_manual_commit_and_idempotent_close(context, clock):
    async def run():
        adapter = ScriptedAdapter(context, clock)
        with pytest.raises(AdapterStateError):
            await adapter.send_audio(frame())
        await adapter.connect()
        with pytest.raises(AdapterStateError):
            await adapter.connect()
        await adapter.configure(config())
        receipt = await adapter.send_audio(frame())
        assert receipt.byte_count == 640
        await adapter.commit_turn("t1")
        with pytest.raises(AdapterStateError):
            await adapter.commit_turn("t1")
        with pytest.raises(AdapterStateError):
            await adapter.send_audio(frame())
        await asyncio.gather(adapter.close(), adapter.close())
        assert adapter.state == "closed" and adapter.close_calls == 1
        with pytest.raises(EOFError):
            await adapter.receive_event()

    asyncio.run(run())


def test_server_vad_never_sends_manual_commit(context, clock):
    async def run():
        adapter = ScriptedAdapter(context, clock)
        await adapter.connect()
        await adapter.configure(config("server_vad"))
        await adapter.send_audio(frame())
        with pytest.raises(AdapterStateError, match="manual"):
            await adapter.commit_turn("t1")
        assert adapter.commits == []
        await adapter.close()

    asyncio.run(run())


def test_receive_has_single_consumer_and_close_wakes_waiter(context, clock):
    async def run():
        adapter = ScriptedAdapter(context, clock)
        await adapter.connect()
        receiving = asyncio.create_task(adapter.receive_event())
        await asyncio.sleep(0)
        with pytest.raises(AdapterStateError, match="one concurrent"):
            await adapter.receive_event()
        await adapter.close()
        with pytest.raises(EOFError):
            await receiving

    asyncio.run(run())


def test_cancel_receipt_does_not_invent_detection_event(context, clock):
    async def run():
        adapter = ScriptedAdapter(context, clock)
        await adapter.connect()
        await adapter.configure(config())
        result = await adapter.interrupt(
            InterruptRequest(target_response_id="r1", reason="control probe")
        )
        assert result.command_id and adapter.queue.empty()
        assert adapter.capabilities().get("native_interrupt").status == "unknown"
        await adapter.close()

    asyncio.run(run())


def test_unknown_capabilities_are_not_silently_supported():
    with pytest.raises(UnsupportedCapability, match="unknown"):
        CapabilityManifest().require("audio_output")


def test_closing_during_connect_cannot_reopen_adapter(context, clock):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        class SlowAdapter(ScriptedAdapter):
            async def _connect(self):
                entered.set()
                await release.wait()
                return await super()._connect()

        adapter = SlowAdapter(context, clock)
        connecting = asyncio.create_task(adapter.connect())
        await entered.wait()
        await adapter.close()
        release.set()
        with pytest.raises(AdapterStateError):
            await connecting
        assert adapter.state == "closed"

    asyncio.run(run())
