"""Reproducible measurement settings, separate from vendor model configuration."""

from typing import Annotated, Literal

from pydantic import Field

from benchmark.contracts import Contract, NonNegativeInt, PositiveInt


class LatencyProfile(Contract):
    version: Literal["latency_v0_1"] = "latency_v0_1"
    max_send_lateness_ms: Annotated[float, Field(gt=0)] = 20.0
    max_send_duration_ms: Annotated[float, Field(gt=0)] = 20.0
    max_playback_lateness_ms: Annotated[float, Field(gt=0)] = 20.0
    tail_silence_ms: NonNegativeInt = 1600
    playback_chunk_ms: PositiveInt = 20
    artifact_queue_capacity: PositiveInt = 4096
    artifact_max_bytes: PositiveInt = 32 * 1024 * 1024
