"""Survey a band with an RTL-SDR and score what is in it.

Nothing is retuned or locked: this only reports. Two stages, the way you would
do it by hand:

  1. rtl_power sweeps the whole segment in a few seconds -> narrow peaks
  2. rtl_fm dwells a few seconds on the best candidates -> is it keyed? what
     speed? what note?

The strongest signal in a CW segment is usually not CW - it is a data mode, a
broadcast spur, or the noise floor of an overloaded front end. So candidates are
ranked by *narrowness* first (how far a bin stands above its neighbours), and
only then probed for keying.
"""
import os
import statistics
import subprocess
import tempfile
import time

from . import autotune, sdr, wpm

# CW segments worth scanning; --start/--stop override in kHz
BANDS = {
    "80m": (3_500_000, 3_600_000),
    "40m": (7_000_000, 7_125_000),
    "30m": (10_100_000, 10_150_000),
    "20m": (14_000_000, 14_070_000),
    "17m": (18_068_000, 18_100_000),
    "15m": (21_000_000, 21_100_000),
    "10m": (28_000_000, 28_100_000),
}

PROBE_RATE = 8000           # same rate the rest of the tool analyses at: the
                            # 5 ms envelope windows need it for tone resolution


def band_range(band=None, start_khz=None, stop_khz=None):
    """(start_hz, stop_hz, label) from a preset name or explicit kHz bounds."""
    if start_khz or stop_khz:
        if not (start_khz and stop_khz):
            raise ValueError("give both --start and --stop (kHz), or a --band")
        lo, hi = int(start_khz * 1000), int(stop_khz * 1000)
        if hi <= lo:
            raise ValueError("--stop must be above --start")
        return lo, hi, f"{start_khz:g}-{stop_khz:g} kHz"
    if not band:
        raise ValueError("no band: use --band 40m, or --start/--stop in kHz")
    key = band.lower()
    if key not in BANDS:
        raise ValueError(f"unknown band {band!r}: try one of {', '.join(sorted(BANDS))}")
    lo, hi = BANDS[key]
    return lo, hi, f"{key} CW ({lo/1e6:.3f}-{hi/1e6:.3f} MHz)"


def survey(start_hz, stop_hz, device=None, ppm=0, gain="auto", hf=False,
           bias_tee=False, seconds=3.0, step_hz=2000, bins=None):
    """Run rtl_power once and return the strongest narrow peaks.

    Each peak is (hz, db, prominence_db): prominence is how far the bin stands
    above its neighbours, which is what separates a real carrier from a broad
    rise in the noise floor.
    """
    exe = sdr.binary("rtl_power")
    if not exe:
        raise RuntimeError("rtl_power not found - it ships with the rtl-sdr package")
    out = tempfile.NamedTemporaryFile(suffix=".csv", delete=False, prefix="scan-")
    path = out.name
    out.close()
    cmd = [exe, "-f", f"{int(start_hz)}:{int(stop_hz)}:{int(step_hz)}",
           "-i", "1", "-e", str(int(max(1, seconds)))]
    if device not in (None, ""):
        cmd += ["-d", str(device)]
    if hf:
        cmd += ["-E", "direct2", "-g", "0"]
    elif gain not in (None, "", "auto"):
        cmd += ["-g", str(gain)]
    if bias_tee:
        cmd += ["-T"]
    cmd.append(path)
    try:
        done = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=seconds + 25)
        if done.returncode != 0:
            detail = (done.stderr or "").strip().splitlines()
            raise RuntimeError("rtl_power failed: "
                               + (detail[-1] if detail else f"exit {done.returncode}"))
        rows = _read_power_csv(path)
        if not rows:
            raise RuntimeError("rtl_power produced no data - is a dongle attached "
                               "and not held by the DVB driver?")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return _peaks(rows, limit=bins or 8)


def _read_power_csv(path):
    """rtl_power CSV -> {hz: [db, ...]} using the middle sample of each sweep."""
    rows = {}
    try:
        with open(path, errors="replace") as fh:
            for line in fh:
                parts = line.strip().split(",")
                if len(parts) < 7:
                    continue
                try:
                    low, high, step = int(parts[2]), int(parts[3]), int(parts[4])
                    values = [float(v) for v in parts[6:] if v.strip()]
                except ValueError:
                    continue
                if not values:
                    continue
                centre = (low + high) / 2.0
                db = max(values)          # the sweep's strongest moment
                rows.setdefault(int(centre), []).append(db)
    except OSError:
        return {}
    return {hz: max(vals) for hz, vals in rows.items()}


def _peaks(bins, limit=8, prominence_db=6.0, max_bins=5, window=10):
    """Narrow groups of bins that stand above the local noise.

    A carrier can occupy two or three adjacent bins (filter shape, FFT leakage),
    so bins above the floor are taken as a *cluster*: the cluster's width rejects
    a wide signal, and its peak must stand clear of the bins just outside it.
    Judging a carrier by its immediate neighbour fails for a flat-topped signal,
    which is why the baseline is taken outside the cluster.
    """
    if not bins:
        return []
    freqs = sorted(bins)
    values = sorted(bins.values())
    floor = values[max(0, int(len(values) * 0.2))]
    step = statistics.median([b - a for a, b in zip(freqs, freqs[1:])]) or 1
    cut = floor + 6.0

    clusters, current = [], []
    for hz in freqs:
        if bins[hz] >= cut:
            if current and hz - current[-1] > step * 1.5:
                clusters.append(current)
                current = []
            current.append(hz)
        elif current:
            clusters.append(current)
            current = []
    if current:
        clusters.append(current)

    out = []
    for cluster in clusters:
        if len(cluster) > max_bins:
            continue                                   # a broad rise, not a carrier
        i0, i1 = freqs.index(cluster[0]), freqs.index(cluster[-1])
        outside = ([bins[freqs[j]] for j in range(max(0, i0 - window), i0)]
                   + [bins[freqs[j]] for j in range(i1 + 1, min(len(freqs), i1 + window + 1))])
        if not outside:
            continue
        peak_hz = max(cluster, key=lambda h: bins[h])
        peak_db = bins[peak_hz]
        prom = peak_db - statistics.median(outside)
        if prom < prominence_db:
            continue
        out.append((peak_hz, round(peak_db, 1), round(prom, 1)))

    out.sort(key=lambda row: (row[2], row[1]), reverse=True)
    kept = []
    for hz, db, prom in out:                           # one per ~400 Hz
        if all(abs(hz - k[0]) > 400 for k in kept):
            kept.append((hz, db, prom))
        if len(kept) >= limit:
            break
    return kept


def probe(freq_hz, seconds=8.0, device=None, ppm=0, gain="auto", hf=False,
          bias_tee=False, mode="usb"):
    """Listen to one frequency for a few seconds and say what is there."""
    cmd = sdr.build_cmd(freq_hz, mode=mode, device=device, ppm=ppm, gain=gain,
                        hf=hf, bias_tee=bias_tee, sample_rate=PROBE_RATE)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    need = int(PROBE_RATE * 2 * seconds)
    chunks, got = [], 0
    deadline = time.time() + seconds + 5
    try:
        while got < need and time.time() < deadline:
            block = proc.stdout.read(8192)
            if not block:
                break
            chunks.append(block)
            got += len(block)
    finally:
        proc.kill()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
    data = b"".join(chunks)
    info = {"freq_hz": int(freq_hz), "level_dbfs": wpm.peak_dbfs(data)}
    choice = autotune.choose(data, rate=PROBE_RATE)
    tone = choice.get("tone_hz")
    info["tone_hz"] = tone
    info["by"] = choice.get("how")
    if tone:
        m = wpm.measure(data, rate=PROBE_RATE, tone_hz=tone)
        if "wpm" in m:
            info.update(wpm=m["wpm"], dot_ms=m.get("dot_ms"),
                        confidence=m.get("confidence"))
    return info


def scan(band=None, start_khz=None, stop_khz=None, top=6, dwell=8.0,
         verbose=None, seconds=3.0, device=None, ppm=0, gain="auto", hf=False,
         bias_tee=False, mode="usb"):
    """Survey, then probe the best candidates. Returns a list of dicts."""
    start, stop, label = band_range(band, start_khz, stop_khz)
    say = verbose or (lambda _msg: None)
    say(f"surveying {label} with rtl_power ({seconds:g} s)")
    peaks = survey(start, stop, device=device, ppm=ppm, gain=gain, hf=hf,
                   bias_tee=bias_tee, seconds=seconds)
    if not peaks:
        say("no peaks above the noise floor in that segment")
        return []
    results = []
    for hz, db, prom in peaks[:top]:
        say(f"  probing {hz/1e6:.4f} MHz  (peak {db:+.0f} dB, {prom:+.0f} dB above "
            f"neighbours) - {dwell:g} s")
        results.append(probe(hz, seconds=dwell, device=device, ppm=ppm, gain=gain,
                             hf=hf, bias_tee=bias_tee, mode=mode))
    return results


def report(results, out=print):
    """Print the survey table."""
    if not results:
        out("  nothing to report")
        return
    out("")
    out("   #   frequency      level    tone   keying")
    out("  " + "-" * 56)
    for i, r in enumerate(results, 1):
        mhz = r["freq_hz"] / 1e6
        lvl = f"{r.get('level_dbfs', -120):.0f} dBFS"
        tone = f"{r['tone_hz']} Hz" if r.get("tone_hz") else "-"
        if r.get("wpm"):
            keying = f"{r['wpm']:.1f} WPM (conf {r.get('confidence', 0):.2f})"
        elif r.get("tone_hz"):
            keying = "carrier, no keying"
        else:
            keying = "no CW"
        out(f"  {i:>2}   {mhz:>10.4f}   {lvl:>8}   {tone:>7}   {keying}")
    out("")
    out("  survey only - nothing was retuned. To work one of these:")
    best = max(results, key=lambda r: (bool(r.get("wpm")), r.get("confidence") or 0))
    out(f"    morse-logger sdr start --freq {best['freq_hz']/1e6:.4f} --mode usb")
    if best.get("tone_hz"):
        out(f"    morse-logger tune --tone {best['tone_hz']}")
    if best.get("wpm"):
        out(f"    morse-logger wpm --set {round(best['wpm'])}")
