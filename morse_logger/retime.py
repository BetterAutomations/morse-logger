"""Re-time CW audio to standard spacing so a normal decoder can read it.

fldigi's CW decoder loses sync across the long gaps of Farnsworth-spaced code
(W1AW's 5 WPM practice: 14 WPM elements with 1.5 s between letters). The element
timing itself is fine - it is only the *spacing* that breaks the decoder.

This module measures every key-down and key-up run, classifies them as
dot/dash and element/letter/word separator, and re-emits a clean tone at the
standard ratios (1 dot element gap, 3 dot letter gap, 7 dot word gap) at the
same character speed. The result decodes like ordinary CW.
"""
import math
import struct

from . import wpm as _wpm

DOT_MIN_S = _wpm.DOT_MIN_S


def _runs(data, rate, tone, window_ms=4):
    """Key-down/key-up runs for the whole recording.

    Deliberately *not* split into blocks: a block boundary cuts a gap in half,
    and the two halves then look like element gaps, so letters merge. The level
    in a recording is steady enough that one envelope serves for all of it.
    """
    env, win = _wpm._band_envelope(data, rate, tone, window_ms, 3)
    if len(env) < 20:
        return []
    ordered = sorted(env)
    noise, peak = ordered[int(len(ordered) * 0.2)], ordered[-1]
    if peak < 400 or peak < noise * 1.6:
        return []
    step = win / 1000.0
    return [(on, d) for on, d in _wpm._crossings(env, noise + 0.5 * (peak - noise), step)
            if d >= DOT_MIN_S]


def measure(data, rate=8000, tone=750.0):
    """Return (runs, dot_s) for the audio, or (None, None) if nothing keyed."""
    runs = _runs(data, rate, tone)
    marks = [d for on, d in runs if on]
    if len(marks) < 8:
        return None, None
    step = 0.004
    bins = {}
    for m in marks:
        b = max(1, round(m / step))
        bins[b] = bins.get(b, 0) + 1
    need = max(3, int(0.12 * len(marks)))
    solid = sorted(b for b, n in bins.items() if n >= need)
    if not solid:
        return None, None
    dot = solid[0] * step
    return runs, dot


def gap_thresholds(runs, dot):
    """(letter_gap, word_gap) in seconds, clustered from the data.

    Standard CW uses 1/3/7 dots, but Farnsworth stretches the separators (18 and
    42 dots for W1AW 5 WPM). Letters outnumber words, so the median separator is
    a letter gap; anything clearly larger is a word gap.
    """
    gaps = sorted(d for on, d in runs if not on and d >= 2.0 * dot)
    if len(gaps) < 4:
        return 3.0 * dot, 7.0 * dot
    # Anything longer than an element gap separates letters. Putting the letter
    # boundary *at* the median (the typical letter gap) makes half of them fail
    # the comparison and merge into the next letter.
    mid = gaps[len(gaps) // 2]
    return 2.0 * dot, 1.8 * mid


def retime(data, rate=8000, tone=750.0, dot_s=None, amplitude=0.30):
    """Return (pcm, stats). pcm is s16le mono at `rate`, standard spacing."""
    runs, dot = measure(data, rate, tone)
    if not runs:
        return None, {"error": "no keying found"}
    dot = dot_s or dot
    letter_gap, word_gap = gap_thresholds(runs, dot)
    step = 1.0 / rate
    out = []
    ramp = int(0.005 * rate)                      # 5 ms raised-cosine edges

    def tone_block(dur):
        n = max(1, int(dur * rate))
        block = []
        for i in range(n):
            v = amplitude * math.sin(2 * math.pi * tone * i * step)
            if i < ramp:
                v *= 0.5 - 0.5 * math.cos(math.pi * i / ramp)
            elif i > n - ramp:
                v *= 0.5 - 0.5 * math.cos(math.pi * (n - i) / ramp)
            block.append(v)
        return block

    def silence(dur):
        return [0.0] * max(0, int(dur * rate))

    stats = {"marks": 0, "dots": 0, "dashes": 0, "letters": 0, "words": 0}
    for on, dur in runs:
        if on:
            is_dot = dur < 2.0 * dot
            out += tone_block(dot if is_dot else 3.0 * dot)
            stats["dots" if is_dot else "dashes"] += 1
            stats["marks"] += 1
        else:
            if dur >= word_gap:
                out += silence(7.0 * dot)
                stats["words"] += 1
            elif dur >= letter_gap:
                out += silence(3.0 * dot)
                stats["letters"] += 1
            else:
                out += silence(dot)
    stats.update({"dot_ms": round(dot * 1000, 1),
                  "letter_gap_ms": round(letter_gap * 1000),
                  "word_gap_ms": round(word_gap * 1000),
                  "in_seconds": round(len(data) / 2 / rate, 1),
                  "out_seconds": round(len(out) / rate, 1)})
    pcm = struct.pack("<%dh" % len(out),
                      *[int(max(-1.0, min(1.0, v)) * 32000) for v in out])
    return pcm, stats
