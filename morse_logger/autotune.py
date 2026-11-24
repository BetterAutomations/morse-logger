"""Find the CW tone in the incoming audio and tell fldigi to decode there.

fldigi's CW decoder is a narrow filter centred on the modem "carrier" (default
1500 Hz).  A WebSDR puts the beat note wherever you tuned it, so we look for a
*steady* narrow-band component between ~300 and ~2600 Hz and move the decoder
there.

A single FFT/Goertzel peak is not enough: a phone QSO or music has plenty of
energy in that range.  What distinguishes CW is that the tone sits on the same
frequency window after window while being keyed on and off.  So we slice the
capture into short windows, find the strongest bin in each, and take the
frequency that most windows agree on.  Stability, not raw energy, is the
discriminator.
"""
import math
import struct

LOW_HZ = 300
HIGH_HZ = 2600
STEP_HZ = 10


def _goertzel(samples, rate, freq):
    """Normalised energy at `freq` (0..1) using a single DFT bin."""
    n = len(samples)
    if n == 0:
        return 0.0
    k = 2.0 * math.cos(2.0 * math.pi * freq / rate)
    s0 = s1 = s2 = 0.0
    for x in samples:
        s0 = x + k * s1 - s2
        s2, s1 = s1, s0
    power = s1 * s1 + s2 * s2 - k * s1 * s2
    return max(0.0, power) / (n * n)


def pcm_to_floats(data):
    n = len(data) // 2
    return [v / 32768.0 for v in struct.unpack("<%dh" % n, data[:n * 2])]


def _prep(samples):
    if not samples:
        return samples
    mean = sum(samples) / len(samples)
    return [s - mean for s in samples]


def spectrum(samples, rate=8000, low=LOW_HZ, high=HIGH_HZ, step=STEP_HZ):
    out = []
    f = low
    while f <= high:
        out.append((f, _goertzel(samples, rate, f)))
        f += step
    return out


def scan(data, rate=8000, low=LOW_HZ, high=HIGH_HZ, step=STEP_HZ):
    """[(freq_hz, energy)] sorted by energy, strongest first."""
    ranked = spectrum(_prep(pcm_to_floats(data)), rate, low, high, step)
    ranked.sort(key=lambda t: t[1], reverse=True)
    return ranked


def spectrum_top(data, rate=8000, n=8):
    return [{"hz": int(f), "energy": round(e, 9)} for f, e in scan(data, rate)[:n]]


KEYED_STEP = 25          # overridden from config on a slow CPU


def scan_keyed(data, rate=8000, low=LOW_HZ, high=HIGH_HZ, step=None, keep=5):
    step = KEYED_STEP if step is None else step
    """Scan the band for a tone that is *keyed*, not merely present.

    Energy is useless as a guide here: a music bed has far more energy than the
    CW note and dominates any peak list, so the CW can sit at 1000 Hz while every
    "strongest bin" is at 300-500 Hz. What distinguishes the CW tone is that its
    own narrow-band envelope goes up and down: it spends time above and below its
    midpoint, with sharp transitions. Steady tones do not; noise does not hold a
    single frequency. So score each candidate by contrast and duty cycle, then
    run the full keying analysis on the shortlist.
    """
    from . import wpm
    shortlist = []
    for tone in range(int(low), int(high) + 1, int(step)):
        env, win = wpm._band_envelope(data, rate, tone, 10, 2)
        if len(env) < 40:
            continue
        ordered = sorted(env)
        noise = ordered[int(len(ordered) * 0.2)]
        peak = ordered[-1]
        if peak < 400 or peak < noise * 1.6:
            continue
        mid = noise + 0.5 * (peak - noise)
        duty = sum(1 for e in env if e > mid) / len(env)
        if not 0.2 <= duty <= 0.8:            # keyed CW is never always-on
            continue
        shortlist.append((peak / max(1, noise), tone))
    shortlist.sort(reverse=True)
    best = None
    for _contrast, tone in shortlist[:keep]:
        env, win = wpm._band_envelope(data, rate, tone, 5, 2)
        info = wpm._analyse(env, win, band=tone, edge_loss=2 * (win / 1000.0))
        if not info or "wpm" not in info:
            continue
        score = (info["confidence"], info.get("contrast", 0), info["marks"])
        if best is None or score > best[0]:
            best = (score, tone, info)
    if not best:
        return None, None
    tone = refine_keyed(data, rate, best[1])
    return tone, best[2]


REFINE_STEP = 10         # overridden from config on a slow CPU


def refine_keyed(data, rate=8000, tone=0, span=60, step=None):
    """Local search for the exact keyed tone, on keying quality not energy."""
    step = REFINE_STEP if step is None else step
    from . import wpm
    best = None
    for cand in range(int(tone - span), int(tone + span) + 1, step):
        env, win = wpm._band_envelope(data, rate, cand, 5, 2)
        info = wpm._analyse(env, win, band=cand, edge_loss=2 * (win / 1000.0))
        if not info or "wpm" not in info:
            continue
        # contrast breaks ties: the exact tone has the deepest gaps between elements
        score = (info["confidence"], info.get("contrast", 0), info["marks"])
        if best is None or score > best[0]:
            best = (score, cand)
    return best[1] if best else tone


def keying_candidates(data, rate=8000, tones=(), top=6):
    """Backwards-compatible wrapper: try the given tones, else scan the band."""
    from . import wpm
    got_tone, got_info = None, None
    for tone in [int(round(t)) for t in tones]:
        env, win = wpm._band_envelope(data, rate, tone, 5, 2)
        info = wpm._analyse(env, win, band=tone, edge_loss=2 * (win / 1000.0))
        if info and "wpm" in info and (got_info is None or info["confidence"] > got_info["confidence"]):
            got_tone, got_info = tone, info
    if got_info and got_info["confidence"] >= 0.7:
        return got_tone, got_info
    tone, info = scan_keyed(data, rate)
    if info and (got_info is None or info["confidence"] > got_info["confidence"]):
        return tone, info
    return got_tone, got_info



def level(data):
    """Rough RMS of s16le audio, for telling "silent" from "no CW in it"."""
    import struct
    n = len(data) // 2
    if n == 0:
        return 0.0
    vals = struct.unpack("<%dh" % n, data[:n * 2])
    return (sum(v * v for v in vals) / n) ** 0.5


def choose(data, rate=8000):
    """Best tone to tune to, with how it was found.

    Two different signals need two different tests: a quiet band gives a *steady*
    carrier (pick), while a fading or crowded one gives a *keyed* tone whose
    energy wanders (scan_keyed). Trying only the first is why "no steady CW tone"
    appeared on a signal that was plainly there.
    """
    info = pick(data) or {}
    if info.get("usable"):
        return dict(info, tone_hz=info["tone_hz"], how="steady")
    tone, kinfo = keying_candidates(data, rate)
    if tone:
        return {"tone_hz": int(tone), "how": "keyed", "usable": True,
                "stability": (kinfo or {}).get("confidence", 0.0),
                "clipping": info.get("clipping", False), "info": kinfo}
    return {"tone_hz": info.get("tone_hz"), "how": None, "usable": False,
            "stability": info.get("stability", 0.0),
            "clipping": info.get("clipping", False)}


def pick(data, rate=8000, windows=8, tolerance=20, min_stability=0.5,
         min_level=1e-7, check_keying=True):
    """Return the most likely CW tone, or None if nothing steady is present.

    Keys: tone_hz, stability (0..1 fraction of windows agreeing), energy,
    usable (steady + loud enough).
    """
    raw = pcm_to_floats(data)
    peak_level = max(abs(s) for s in raw) if raw else 0.0
    samples = _prep(raw)
    if len(samples) < rate // 2:
        return None
    wlen = len(samples) // windows
    if wlen < rate // 10:
        windows = max(2, len(samples) // (rate // 10))
        wlen = len(samples) // windows
    peaks = []
    for i in range(windows):
        chunk = samples[i * wlen:(i + 1) * wlen]
        spec = spectrum(chunk, rate)
        peak = max(spec, key=lambda t: t[1])
        if peak[1] > min_level:
            peaks.append(peak)
    if not peaks:
        return None
    # vote: how many windows agree with the best-supported candidate
    best = None
    for freq, energy in peaks:
        agree = [p for p in peaks if abs(p[0] - freq) <= tolerance]
        score = (len(agree), energy)
        if best is None or score > best[0]:
            best = (score, freq, agree)
    (_count, _energy), freq, agree = best
    stability = len(agree) / len(peaks)
    mean_energy = sum(p[1] for p in agree) / len(agree)
    # A runaway feedback loop (or a hot sound card) pins the input at full
    # scale; every bin then looks "steady" and the decoder hears mush.
    clipped = peak_level >= 0.98
    out = {
        "tone_hz": int(freq),
        "stability": round(stability, 2),
        "energy": mean_energy,
        "peak": round(peak_level, 3),
        "clipping": clipped,
        "usable": (not clipped) and stability >= min_stability and mean_energy >= min_level,
    }
    if check_keying and out["usable"]:
        # Does this tone look like Morse, or is something else steady here?
        tone, info = keying_candidates(data, rate, tones=[int(freq)])
        if info and tone != int(freq):
            out["steady_at_hz"] = int(freq)
            out["tone_hz"] = tone
            out["keyed_wpm"] = info["wpm"]
            out["keyed_confidence"] = info["confidence"]
        elif info:
            out["keyed_wpm"] = info["wpm"]
            out["keyed_confidence"] = info["confidence"]
    return out
