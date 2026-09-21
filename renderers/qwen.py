"""Qwen TTS renderer used only before a timed model session is opened."""

import subprocess

from adapters.qwen.tts import synthesize
from benchmark.audio import AudioFormat
from renderers.base import RenderedText, TTSRenderer

RENDERER_VERSION = "qwen-tts-renderer-0.1"


class QwenTTSRenderer(TTSRenderer):
    def __init__(self, *, model: str, voice: str, language: str, sample_rate_hz: int):
        self.model = model
        self.voice = voice
        self.language = language
        self.format = AudioFormat(sample_rate_hz=sample_rate_hz)
        self._ffmpeg_version: str | None = None

    def fingerprint(self) -> dict:
        if self._ffmpeg_version is None:
            result = subprocess.run(
                ["ffmpeg", "-version"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            self._ffmpeg_version = result.stdout.decode("utf-8", "replace").splitlines()[0]
        return {
            "renderer_version": RENDERER_VERSION,
            "provider": "qwen",
            "model": self.model,
            "voice": self.voice,
            "language": self.language,
            "format": self.format.model_dump(mode="json"),
            "decoder": self._ffmpeg_version,
        }

    def synthesize(self, text: str) -> RenderedText:
        encoded, metadata = synthesize(text, model=self.model, voice=self.voice)
        result = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                "pipe:0",
                "-ac",
                str(self.format.channels),
                "-ar",
                str(self.format.sample_rate_hz),
                "-f",
                "s16le",
                "pipe:1",
            ],
            input=encoded,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=90,
        )
        if result.returncode or not result.stdout:
            raise RuntimeError("ffmpeg could not normalize the TTS response")
        if len(result.stdout) % self.format.bytes_per_sample_frame:
            raise ValueError("TTS decoder returned partial sample frames")
        return RenderedText(result.stdout, self.format, metadata)
