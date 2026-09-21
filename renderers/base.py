"""TTS renderer contract. Renderers return normalized PCM, never benchmark events."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from benchmark.audio import AudioFormat


@dataclass(frozen=True)
class RenderedText:
    pcm: bytes
    format: AudioFormat
    provider_metadata: dict[str, Any]


class TTSRenderer(ABC):
    @abstractmethod
    def fingerprint(self) -> dict[str, Any]: ...

    @abstractmethod
    def synthesize(self, text: str) -> RenderedText: ...
