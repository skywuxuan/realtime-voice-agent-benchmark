"""Explicit local Qwen3-Omni deployment metadata contract."""

from pydantic import BaseModel, Field


class QwenOmniBackendProfile(BaseModel):
    backend: str = Field(description="transformers, vllm-omni, or another verified backend")
    backend_commit: str
    model_revision: str
    device: str
    gpu_count: int = Field(gt=0)
    dtype: str
    streaming: bool
    audio_input_format: str
    audio_output_format: str

    def supports_realtime_benchmark(self) -> bool:
        return (
            self.streaming
            and self.audio_input_format == "pcm_s16le"
            and self.audio_output_format == "pcm_s16le"
        )
