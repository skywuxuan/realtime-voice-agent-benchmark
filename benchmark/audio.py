"""PCM contracts shared by adapters, artifacts and scenarios."""

from typing import Literal

from pydantic import model_validator

from benchmark.contracts import (
    Contract,
    Identifier,
    NonNegativeInt,
    PositiveInt,
    RelativePath,
)


class AudioFormat(Contract):
    encoding: Literal["pcm_s16le"] = "pcm_s16le"
    sample_rate_hz: PositiveInt
    channels: PositiveInt = 1

    @property
    def bytes_per_sample_frame(self) -> int:
        return self.channels * 2


class AudioRef(AudioFormat):
    path: RelativePath
    byte_offset: NonNegativeInt
    byte_length: PositiveInt
    sample_offset: NonNegativeInt
    sample_count: PositiveInt

    @model_validator(mode="after")
    def check_offsets(self) -> "AudioRef":
        width = self.bytes_per_sample_frame
        if self.byte_offset != self.sample_offset * width:
            raise ValueError("byte_offset does not match sample_offset")
        if self.byte_length != self.sample_count * width:
            raise ValueError("byte_length does not match sample_count")
        return self


class AudioFrame(Contract):
    pcm: bytes
    format: AudioFormat
    stream_id: Identifier
    turn_id: Identifier
    chunk_index: NonNegativeInt
    sample_offset: NonNegativeInt

    @model_validator(mode="after")
    def check_pcm(self) -> "AudioFrame":
        if not self.pcm or len(self.pcm) % self.format.bytes_per_sample_frame:
            raise ValueError("PCM must contain whole, nonempty sample frames")
        return self

    @property
    def sample_count(self) -> int:
        return len(self.pcm) // self.format.bytes_per_sample_frame
