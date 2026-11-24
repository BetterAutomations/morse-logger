#!/usr/bin/env python3
"""Generate a CW (Morse) WAV test signal. Stdlib only."""
import math, struct, sys, wave, random

MORSE = {
    'A': '.-', 'B': '-...', 'C': '-.-.', 'D': '-..', 'E': '.', 'F': '..-.',
    'G': '--.', 'H': '....', 'I': '..', 'J': '.---', 'K': '-.-', 'L': '.-..',
    'M': '--', 'N': '-.', 'O': '---', 'P': '.--.', 'Q': '--.-', 'R': '.-.',
    'S': '...', 'T': '-', 'U': '..-', 'V': '...-', 'W': '.--', 'X': '-..-',
    'Y': '-.--', 'Z': '--..', '0': '-----', '1': '.----', '2': '..---',
    '3': '...--', '4': '....-', '5': '.....', '6': '-....', '7': '--...',
    '8': '---..', '9': '----.', '.': '.-.-.-', ',': '--..--', '?': '..--..',
    '/': '-..-.', '=': '-...-', '+': '.-.-.', ':': '---...', "'": '.----.',
    '-': '-....-', '(': '-.--.', ')': '-.--.-', '@': '.--.-.',
}


def text_to_timing(text, wpm=20):
    """Return list of (on_seconds, off_seconds) keying segments."""
    dot = 1.2 / wpm
    segs = []
    for i, word in enumerate(text.upper().split(' ')):
        for j, ch in enumerate(word):
            code = MORSE.get(ch)
            if not code:
                continue
            for k, sym in enumerate(code):
                on = dot if sym == '.' else 3 * dot
                off = dot if k < len(code) - 1 else 3 * dot  # inter-char gap
                segs.append((on, off))
        if i < len(text.split(' ')) - 1:
            # word gap: already 3 dots after last char, add 4 more (total 7)
            segs[-1] = (segs[-1][0], segs[-1][1] + 4 * dot)
    return segs, dot


def synth(text, wpm=20, freq=1500.0, rate=48000, amp=0.35, noise=0.0, rise=0.005, seed=1234):
    """Return list of float samples (mono)."""
    segs, dot = text_to_timing(text, wpm)
    lead = int(rate * 0.3)
    out = [0.0] * lead
    phase = 0.0
    rnd = random.Random(seed)
    for on, off in segs:
        n = int(rate * on)
        ramp = max(1, int(rate * rise))
        for i in range(n):
            env = 1.0
            if i < ramp:
                env = 0.5 - 0.5 * math.cos(math.pi * i / ramp)
            elif i > n - ramp:
                env = 0.5 - 0.5 * math.cos(math.pi * (n - i) / ramp)
            out.append(amp * env * math.sin(phase))
            phase += 2 * math.pi * freq / rate
            if phase > 2 * math.pi:
                phase -= 2 * math.pi
        out.extend([0.0] * int(rate * off))
    out.extend([0.0] * int(rate * 0.3))
    if noise > 0:
        out = [s + rnd.gauss(0, noise) for s in out]
    return out


def write_wav(path, samples, rate=48000):
    with wave.open(path, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for s in samples:
            v = max(-1.0, min(1.0, s))
            frames += struct.pack('<h', int(v * 32767))
        w.writeframes(bytes(frames))


if __name__ == '__main__':
    text = sys.argv[1] if len(sys.argv) > 1 else 'CQ CQ CQ DE N0CALL TEST'
    path = sys.argv[2] if len(sys.argv) > 2 else 'test.wav'
    noise = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0
    write_wav(path, synth(text, noise=noise))
    print(path)
