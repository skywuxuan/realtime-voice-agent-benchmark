"""Capture observation time before queues or disk IO."""

import time
import uuid
from datetime import UTC, datetime
from typing import Protocol

from pydantic import AwareDatetime, field_validator

from benchmark.contracts import Contract, Identifier, NonNegativeInt

_PROCESS_CLOCK_ID = f"process_{uuid.uuid4().hex}"


class ClockReading(Contract):
    clock_id: Identifier
    timestamp_monotonic_ns: NonNegativeInt
    wall_clock_timestamp: AwareDatetime

    @field_validator("wall_clock_timestamp")
    @classmethod
    def utc_timestamp(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class Clock(Protocol):
    def now(self) -> ClockReading: ...


class SystemClock:
    def now(self) -> ClockReading:
        return ClockReading(
            clock_id=_PROCESS_CLOCK_ID,
            timestamp_monotonic_ns=time.monotonic_ns(),
            wall_clock_timestamp=datetime.now(UTC),
        )


def elapsed_ms(start: ClockReading, end: ClockReading) -> float:
    if start.clock_id != end.clock_id:
        raise ValueError("cannot subtract observations from different clock domains")
    # Negative values are evidence of early responses; never clamp them to zero.
    return (end.timestamp_monotonic_ns - start.timestamp_monotonic_ns) / 1_000_000
