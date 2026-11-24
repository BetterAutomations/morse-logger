"""Detect how fast this CPU is, so the watchdogs and scans can be scaled.

Model names lie: a VM reports the host CPU, a throttled laptop runs at a
fraction of its rating, and "Core 2 Duo" tells you nothing about the clock it is
actually holding. So measure the work instead - a fixed integer loop, which is
what the audio analysis is made of anyway.
"""
import json
import os
import time

CACHE = "cpu.json"
BENCH_VERSION = 1          # bump when the workload changes, so old scores retire


def _workload(n=500_000):
    x = 0
    for i in range(n):
        x = (x * 1103515245 + 12345) & 0x7FFFFFFF
    return x


def benchmark(reps=3):
    """Seconds for the fixed workload (best of `reps`, to skip scheduler noise)."""
    best = None
    for _ in range(reps):
        t0 = time.process_time()
        _workload()
        dt = time.process_time() - t0
        best = dt if best is None else min(best, dt)
    return round(best, 4)


def classify(seconds):
    """Map the score to a profile.

    Calibrated on a Core Ultra 9 185H, where the 500k-iteration loop takes
    ~0.028 s. Python single-thread speed tracks clock and IPC closely, so a
    Penryn-era Core 2 Duo SU9300 (1.2 GHz) lands around 0.10-0.15 s and an Atom
    slower still, so both get the slow profile.
    """
    if seconds <= 0.055:
        return "fast"        # anything from the last decade lands here
    if seconds <= 0.100:
        return "medium"      # older dual cores (Core 2 era at full clock)
    return "slow"           # 1.2 GHz Core 2 Duo SU9300, Atom, thin clients


def detect(home, force=False):
    """(score, profile), cached in run/cpu.json so it is measured once."""
    path = os.path.join(home, "run", CACHE)
    if not force:
        try:
            with open(path) as f:
                got = json.load(f)
            if got.get("version") == BENCH_VERSION:
                return got["seconds"], got["profile"]
        except (OSError, ValueError, KeyError):
            pass
    seconds = benchmark()
    profile = classify(seconds)
    try:
        # never create the directory here: this runs during load_config, and a
        # read-only command must not resurrect a directory the user deleted
        if os.path.isdir(os.path.dirname(path)):
            with open(path, "w") as f:
                json.dump({"version": BENCH_VERSION, "seconds": seconds,
                           "profile": profile, "when": time.time()}, f, indent=2)
                f.write("\n")
    except OSError:
        pass
    return seconds, profile
