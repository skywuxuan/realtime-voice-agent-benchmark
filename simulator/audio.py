"""PCM timeline utilities, used for reproducible assets and artifact rendering."""

import array
import io
import math
import sys
import wave
from pathlib import Path

from benchmark.audio import AudioFormat


def read_wav(path: Path, expected: AudioFormat) -> bytes:
    return read_wav_bytes(path.read_bytes(), expected)


def read_wav_bytes(data: bytes, expected: AudioFormat) -> bytes:
    with wave.open(io.BytesIO(data), "rb") as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype()) != (
            expected.channels,
            2,
            expected.sample_rate_hz,
            "NONE",
        ):
            raise ValueError("WAV must match the declared PCM16 format")
        pcm = wav.readframes(wav.getnframes())
        if len(pcm) != wav.getnframes() * expected.bytes_per_sample_frame:
            raise ValueError("truncated WAV data")
        return pcm


def wav_bytes(pcm: bytes, format: AudioFormat) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(format.channels)
        wav.setsampwidth(2)
        wav.setframerate(format.sample_rate_hz)
        wav.writeframes(pcm)
    return output.getvalue()


def estimate_speech_bounds(
    pcm: bytes, sample_rate: int, *, window_ms: int = 20
) -> tuple[tuple[int, int], dict]:
    """Energy endpoint estimate, explicitly NOT a human-verified speech annotation."""
    samples = array.array("h", pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    width = sample_rate * window_ms // 1000
    if not samples or width < 1:
        raise ValueError("nonempty PCM and a valid window size are required")
    energy = [
        math.sqrt(sum(v * v for v in samples[i : i + width]) / len(samples[i : i + width]))
        for i in range(0, len(samples), width)
    ]
    threshold = max(24.0, max(energy) * 0.01)
    active = [index for index, value in enumerate(energy) if value >= threshold]
    if not active:
        raise ValueError("no speech-energy region found")
    # One extra window guards quiet onsets/offsets; semantic error is not bounded by this window.
    first = max(0, (active[0] - 1) * width)
    end = min(len(samples), (active[-1] + 2) * width)
    return (first, end), {
        "window_ms": window_ms,
        "threshold_pcm_rms": threshold,
        "guard_windows": 1,
        "semantic_error_bound": None,
    }


def render_timeline(
    segments: list[tuple[int, bytes]], *, origin_ns: int, format: AudioFormat
) -> bytes:
    """Render sample intervals with explicit silence gaps. Reject overlaps instead of mixing them."""
    width = format.bytes_per_sample_frame
    output = bytearray()
    last_end = 0
    for timestamp, pcm in sorted(segments, key=lambda item: item[0]):
        sample = round((timestamp - origin_ns) * format.sample_rate_hz / 1_000_000_000)
        if sample < 0 or sample < last_end:
            raise ValueError("timeline intervals overlap or precede their origin")
        output.extend(b"\0" * ((sample - last_end) * width))
        output.extend(pcm)
        last_end = sample + len(pcm) // width
    return bytes(output)
