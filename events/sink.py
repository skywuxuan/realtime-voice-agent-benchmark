"""Artifact capabilities available to adapters, without a concrete recorder dependency."""

from typing import Protocol

from benchmark.audio import AudioFormat, AudioRef
from events.schema import RawEvent


class AdapterArtifactSink(Protocol):
    async def record_raw(self, raw: RawEvent) -> RawEvent: ...

    async def store_audio(self, stream_id: str, pcm: bytes, format: AudioFormat) -> AudioRef: ...
