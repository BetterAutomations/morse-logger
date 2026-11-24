"""Measure the sender's keying speed (WPM) from the received audio.

fldigi's CW decoder needs to know roughly how fast the incoming fist is. If it
assumes a faster speed than the sender uses, normal gaps *inside* a word look
like word gaps and you get "N O W  ...  YO U  ...  WE E K". Too slow an
assumption merges letters instead. There is no XML-RPC call to change the speed
at run time, so the practical answer is: measure the fist, set it, restart.

Method: envelope the audio, find key-down runs, and take the shortest runs as
the dot length (dashes are three dots; a character gap is three dots). Median
of the shortest quarter of marks is a stable estimate for hand-sent CW. Runs on
a 4 ms grid, so it is cheap enough for a Raspberry Pi.
"""
import math
import struct

DOT_MIN_S = 0.012      # ignore click artefacts shorter than this


def _goertzel(seg, rate, freq):
    """Magnitude of one frequency in a short window (cheap, no FFT)."""
    k = int(round(len(seg) * freq / rate))
    w = 2.0 * 3.141592653589793 * k / len(seg)
    c = 2.0 * __import__("math").cos(w)
    s1 = s2 = 0.0
    for x in seg:
        s0 = x + c * s1 - s2
        s2, s1 = s1, s0
    real = s1 - s2 * __import__("math").cos(w)
    imag = s2 * __import__("math").sin(w)
    return (real * real + imag * imag) ** 0.5 / max(1, len(seg))


def narrowness(data, rate=8000, tone_hz=800.0, win_ms=100):
    """How concentrated is the energy at `tone_hz`? CW is a narrow carrier.

    Voice and music spread energy across hundreds of Hz, so an amplitude envelope
    centred on them still rises and falls at syllable rates and looks deceptively
    like keying. Measuring over long windows (10 Hz resolution) shows the
    difference: a real carrier puts nearly all of its energy in one bin, speech
    does not. Returns 0..1.
    """
    n = len(data) // 2
    if n < rate // 2:
        return 0.0
    vals = struct.unpack("<%dh" % n, data[:n * 2])
    w = max(1, int(rate * win_ms / 1000))
    at_tone = near = 0.0
    for i in range(0, n - w, w):
        seg = vals[i:i + w]
        at_tone += _goertzel(seg, rate, tone_hz) ** 2
        side = 0.0
        for f in (tone_hz - 150, tone_hz - 100, tone_hz + 100, tone_hz + 150):
            if f > 100:
                side += _goertzel(seg, rate, f) ** 2
        near += side
    total = at_tone + near
    return (at_tone / total) if total else 0.0


def _band_envelope(data, rate=8000, tone_hz=800.0, win_ms=10, smooth=3):
    """Keying envelope of ONE tone - immune to music and speech around it.

    Broadband amplitude only works on a quiet band. A YouTube video, a voice in
    the background, or a music bed never goes silent, so the envelope never
    drops and every transient looks like a mark. Measuring the energy at the
    decoder's own CW tone instead recovers the keying.
    """
    n = len(data) // 2
    if n < rate // 2:
        return [], win_ms
    vals = struct.unpack("<%dh" % n, data[:n * 2])
    w = max(1, int(rate * win_ms / 1000))
    raw = []
    for i in range(0, n - w, w):
        raw.append(_goertzel(vals[i:i + w], rate, tone_hz))
    # 3-window moving average: a real element lasts many windows, a noise spike
    # lasts one. Without this the band envelope is all transients.
    env = []
    half = max(0, smooth // 2)
    for i in range(len(raw)):
        seg = raw[max(0, i - half):i + half + 1]
        env.append(int(sum(seg) / len(seg)))
    return env, win_ms


def _envelope(data, rate=8000, win_ms=4):
    n = len(data) // 2
    if n < rate // 2:
        return [], rate, win_ms
    vals = struct.unpack("<%dh" % n, data[:n * 2])
    w = max(1, int(rate * win_ms / 1000))
    env = []
    for i in range(0, n - w, w):
        seg = vals[i:i + w]
        env.append(max(abs(v) for v in seg))
    return env, rate, win_ms


def _crossings(env, mid, step):
    """[(is_on, duration_seconds)] found at mid-level crossings.

    Timing from a single threshold is biased: smoothing rounds the element edges
    and the duration depends on where the threshold sits. Crossing the same
    midpoint on the way up and on the way down cancels that, and interpolating
    between windows keeps the resolution finer than the window itself.
    """
    if len(env) < 2:
        return []
    out = []
    prev = env[0] - mid
    on = prev > 0
    entered = 0.0                      # time of the last crossing
    for i in range(1, len(env)):
        cur = env[i] - mid
        if (prev > 0) != (cur > 0):
            span = cur - prev
            frac = (-prev / span) if span else 0.0
            t = (i - 1 + frac) * step
            out.append((on, max(0.0, t - entered)))
            on = cur > 0
            entered = t
        prev = cur
    out.append((on, 0.0))              # trailing partial element
    return out[:-1] or out


def _otsu(env, bins=32):
    """Threshold that best separates marks from gaps.

    The midpoint of noise..peak is right for a clean signal, but when the note is
    only a few dB above the noise it cuts far too high: only the very tips of the
    elements clear it, every mark comes out 15 ms long, and the speed reads as
    noise (80+ WPM). Otsu maximises the variance between the two populations and
    copes with low contrast.
    """
    lo, hi = min(env), max(env)
    if hi <= lo:
        return lo
    hist = [0] * bins
    for v in env:
        b = int((v - lo) / (hi - lo) * (bins - 1))
        hist[b] += 1
    total = len(env)
    sum_all = sum(i * hist[i] for i in range(bins))
    sum_b = w_b = 0
    best_var, best = -1.0, lo + 0.5 * (hi - lo)
    for i in range(bins):
        w_b += hist[i]
        if not w_b:
            continue
        w_f = total - w_b
        if not w_f:
            break
        sum_b += i * hist[i]
        m_b = sum_b / w_b
        m_f = (sum_all - sum_b) / w_f
        var = w_b * w_f * (m_b - m_f) ** 2
        if var > best_var:
            best_var = var
            best = lo + (i + 0.5) * (hi - lo) / (bins - 1)
    return best


def _analyse(env, win_ms, band=None, edge_loss=0.0):
    """Shared element timing for both envelope types."""
    if len(env) < 40:
        return None
    ordered = sorted(env)
    noise = ordered[int(len(ordered) * 0.2)]
    peak = ordered[-1]
    if peak < 400 or peak < noise * 1.6:
        return None                                   # nothing keyed here
    mid = noise + 0.5 * (peak - noise)
    # low contrast: the midpoint keeps only the tips of each element
    if peak < noise * 8:
        mid = max(noise * 1.15, min(mid, _otsu(env)))
    step = win_ms / 1000.0
    runs = _crossings(env, mid, step)
    # A moving average of N windows shortens every element by about N windows:
    # both edges are detected late/early. Put it back, or every speed reads fast.
    marks = [d + edge_loss for on, d in runs if on and d >= DOT_MIN_S]
    gaps = [d for on, d in runs if not on and d >= DOT_MIN_S]
    if len(marks) < 8:
        return {"marks": len(marks), "rejected": f"only {len(marks)} key-down elements in the window - "
                            f"not enough CW to measure"}
    bins = {}
    for m in marks:
        b = max(1, round(m / step))
        bins[b] = bins.get(b, 0) + 1
    need = max(3, int(0.12 * len(marks)))
    solid = sorted(b for b, n in bins.items() if n >= need)
    if not solid:
        return {"marks": len(marks), "rejected": f"element lengths are all different ({len(bins)} distinct "
                            f"values) - this is noise, not keying"}
    base = solid[0]
    lo, hi = base - 1, base + 1
    num = sum(b * bins.get(b, 0) for b in range(lo, hi + 1))
    den = sum(bins.get(b, 0) for b in range(lo, hi + 1))
    dot = (num / den if den else base) * step
    if dot <= 0:
        return None
    if dot < 0.018:                     # faster than 66 WPM: noise clicks
        return {"marks": len(marks), "rejected": f"marks average {dot * 1000:.0f} ms, i.e. "
                            f"{1.2 / dot:.0f} WPM - that is noise, not a fist"}
    wpm_val = 1.2 / dot
    dashes = [m for m in marks if m > dot * 1.8]
    ratio = (sum(dashes) / len(dashes) / dot) if dashes else None
    conf = 0.0
    if ratio is not None:
        conf = max(0.0, 1.0 - abs(ratio - 3.0) / 3.0)
    if ratio is None or abs(ratio - 3.0) > 0.8:
        return {"marks": len(marks), "rejected": f"dash/dot ratio {ratio if ratio is None else round(ratio, 2)} "
                            f"is not near 3 - this does not look like CW keying"}
    dots = [m for m in marks if 0.55 * dot <= m <= 1.5 * dot]
    dashes_c = [m for m in marks if 2.1 * dot <= m <= 3.9 * dot]
    share = (len(dots) + len(dashes_c)) / len(marks)
    if share < 0.7:
        return {"marks": len(marks), "rejected": f"only {share:.0%} of marks are dot- or dash-length - "
                            f"this looks like steady noise at that frequency, not keying"}
    if len(dashes_c) < 2:
        return {"marks": len(marks), "rejected": f"only {len(dashes_c)} dash-length elements - "
                            f"not enough to be CW"}
    if len(dots) >= 3:
        mean = sum(dots) / len(dots)
        spread = (sum((m - mean) ** 2 for m in dots) / len(dots)) ** 0.5 / mean
        if spread > 0.35:
            return {"marks": len(marks), "rejected": f"dot length varies by {spread:.0%} - irregular, "
                                f"more like noise than a hand key"}
    out = {"wpm": round(wpm_val, 1), "dot_ms": round(dot * 1000, 1),
           "marks": len(marks), "dash_ratio": round(ratio, 2),
           "confidence": round(conf, 2),
           "contrast": round(peak / max(1, noise), 2)}
    if band:
        out["band_hz"] = band
    return out



def peak_dbfs(data):
    """Peak level of s16le audio, in dBFS (0 = full scale)."""
    if not data:
        return -120.0
    import array
    import math
    a = array.array("h")
    a.frombytes(data[: len(data) // 2 * 2])
    peak = max((abs(v) for v in a), default=0)
    return round(20 * math.log10(max(1, peak) / 32768.0), 1)


def measure(data, rate=8000, win_ms=None, tone_hz=None, smooth=2,
            thr_hi=0.45, thr_lo=0.20):
    """Return {wpm, dot_ms, marks, confidence} or a {"rejected": reason} dict.

    Two passes. The broadband amplitude envelope is exact on a quiet band, so it
    is tried first. If that finds nothing (music, speech, a video with a
    soundtrack - audio that never goes silent) and the CW tone is known, the
    keying envelope is re-measured at that one frequency. The band-limited pass
    is robust but reads up to ~10% fast, because it integrates over a window and
    a real element's rise time is part of it, so its answer carries
    `band_hz` to say where it came from.
    """
    first = _analyse(*_envelope(data, rate, win_ms or 4)[::2])   # env, win_ms
    if first and "wpm" in first:
        return first
    broadband_reason = first
    if tone_hz:
        narrow = narrowness(data, rate, tone_hz)
        if narrow < 0.20:          # spread across the band: voice/music, not a carrier
            return {"rejected": f"no narrow carrier at {tone_hz:g} Hz "
                                f"(energy is spread out - voice or music, not CW)"}
        env, wms = _band_envelope(data, rate, tone_hz, win_ms or 5, smooth)
        band = _analyse(env, wms, band=tone_hz,
                        edge_loss=smooth * (wms / 1000.0))
        if band and "wpm" in band:
            return band
        if band and not broadband_reason:
            return band
    return broadband_reason or {"rejected": "no audio to analyse"}


def listen_windows(interval_s, max_total=31.0, first=5.0, extra=(10.0, 15.0)):
    """Windows for an adaptive listen that must finish before the next cycle.

    Probing blocks the daemon loop, so an interval of 15 s cannot afford a 30 s
    listen: the budget is the interval minus a small margin, and the last window
    is trimmed to fit rather than overshooting.
    """
    cap = max(first, min(max_total, float(interval_s) - 2.0))
    windows, total = [], 0.0
    if cap >= first:
        windows.append(first)
        total = first
    for want in extra:
        if total + want > cap:
            if cap - total >= 3.0:
                windows.append(round(cap - total, 1))
            break
        windows.append(want)
        total += want
    return tuple(windows)

def measure_live(capture, tone_hz=None, windows=(5, 10, 15), need=8, say=None):
    """Measure the fist, listening longer while there is too little CW.

    A slow sender, a short over, or a deep fade can leave a 5-second window with
    a handful of elements. Rather than refusing, accumulate audio: 5 s, then 15,
    then 30 before giving up. Only a shortage of elements is worth waiting for;
    a noise rejection will not improve with more of the same.
    """
    data = b""
    total = 0
    attempts = 0
    last_level = -120.0
    info = {"rejected": "no audio"}
    tried = []
    # A *shortage* of elements is cured by more audio from the same over, so the
    # window grows. A noise verdict is not about quantity at all: it means this
    # particular stretch was dominated by clicks, a fade, or QRM, and the next
    # stretch may be fine - so that one is retried on fresh audio instead.
    while attempts < 3:
        extra = windows[min(attempts, len(windows) - 1)] if attempts else windows[0]
        chunk = capture(extra)
        if not chunk:
            break
        data += chunk
        last_level = peak_dbfs(chunk)
        total += extra
        attempts += 1
        info = measure(data, rate=8000, tone_hz=tone_hz) or {"rejected": "no audio"}
        if "wpm" in info:
            info["listened_s"] = total
            info["attempts"] = attempts
            if tried and say:
                say(f"  the earlier window read \"{tried[-1][:40]}...\" - this one measured")
            return info
        got = info.get("marks", 0)
        tried.append(info.get("rejected", ""))
        if got < need and "marks" in info:
            if say:
                say(f"  {got} elements in {total} s - listening longer")
            continue
        if say:
            say(f"  window {attempts} was not CW ({info.get('rejected', '')[:46]}...) "
                f"- taking a fresh window")
        data = b""                     # a noise verdict is about content, not length
        if attempts >= 3:
            break
    info["listened_s"] = total
    info["attempts"] = attempts
    level = peak_dbfs(data) if data else last_level
    info["level_dbfs"] = level
    if "rejected" in info and level < -30:
        info["rejected"] = (
            f"the cable is very quiet ({level} dBFS) - turn up the WebSDR or player "
            f"volume (aim for -20 to -10 dBFS); a carrier can be found at this level "
            f"but there is no keying to measure")
    return info
