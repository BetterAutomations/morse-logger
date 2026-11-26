"""Shared helpers for the audio tests (stdlib only, no fixtures needed)."""

import math
import struct

from morse_logger import morse


def s16(samples):
    """Pack float samples (-1..1) as little-endian signed 16-bit PCM."""
    return struct.pack("<%dh" % len(samples),
                       *[max(-32767, min(32767, int(v * 32767))) for v in samples])


def cw(text, wpm=20, tone=750.0, rate=8000, noise=0.0):
    """Synthetic keyed CW as s16le mono at `rate`."""
    return s16(morse.synth(text, wpm=wpm, freq=tone, rate=rate, noise=noise))


def sine(freq, seconds=1.0, rate=8000, amp=0.3):
    """A steady (unkeyed) carrier as s16le mono."""
    n = int(rate * seconds)
    return s16([amp * math.sin(2 * math.pi * freq * i / rate) for i in range(n)])


def silence(seconds=1.0, rate=8000):
    return b"\x00" * int(rate * 2 * seconds)
