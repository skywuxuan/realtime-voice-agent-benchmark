"""Resolve input renderers without coupling them to model adapters."""

from dataset.schema import TTSProfile
from renderers.qwen import QwenTTSRenderer


def create_renderer(profile: TTSProfile):
    if profile.provider == "qwen":
        return QwenTTSRenderer(
            model=profile.model,
            voice=profile.voice,
            language=profile.language,
            sample_rate_hz=profile.sample_rate_hz,
        )
    raise ValueError(f"unsupported TTS provider: {profile.provider}")
