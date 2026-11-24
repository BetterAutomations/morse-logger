"""End-to-end self test: synthetic CW -> virtual cable -> decoder -> log line.

This proves the whole chain without any radio: we generate a Morse WAV with a
known message, play it into the cable, and check what fldigi decoded.
"""
import difflib
import os
import re
import subprocess
import time

from . import fldigi, logger, morse, pulse


def _player(wav, sink=pulse.SINK):
    """Command that plays `wav` into the virtual cable sink (not the speakers)."""
    from shutil import which
    if which("pw-play"):
        return ["pw-play", f"--target={sink}", wav]
    if which("paplay"):
        return ["paplay", f"--device={sink}", wav]
    return None


def _norm(s):
    return re.sub(r"[^A-Z0-9 ]+", "", s.upper()).strip()


def similarity(expected, got):
    a, b = _norm(expected), _norm(got)
    if not a:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def run(cfg, message="CQ CQ CQ DE N0CALL TEST K", wpm=20, noise=0.0,
        tone=None, keep=False, log=print):
    tone = tone or cfg["tone_hz"]
    report = []

    def check(name, ok, detail=""):
        report.append((name, ok, detail))
        log(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
        return ok

    log("morse-logger self test")
    ok = True
    ok &= check("fldigi binary", bool(fldigi.find_binary(cfg.get("fldigi_binary"))),
                fldigi.find_binary(cfg.get("fldigi_binary")) or "not found")
    ok &= check("Xvfb installed", bool(__import__("shutil").which("Xvfb")))
    ok &= check("pulse/pipewire tools", pulse.available(),
                "pactl + parec")
    wav = os.path.join(cfg["_home"], "run", "selftest.wav")
    player = _player(wav)
    ok &= check("audio player", bool(player),
                player[0] if player else "pw-play/paplay missing")
    if not ok:
        return report, False

    log("  creating virtual audio cable ...")
    pulse.ensure_cable(state_file=os.path.join(cfg["_home"], "run", "pulse-modules.json"),
                       monitor=False, tap=False)
    check("cable sink", pulse.sink_exists(), pulse.SINK)
    check("cable source", pulse.source_exists(), pulse.SOURCE)

    os.makedirs(os.path.dirname(wav), exist_ok=True)
    samples = morse.synth(message, wpm=wpm, freq=tone, noise=noise)
    morse.write_wav(wav, samples)
    log(f"  test signal: {message!r} @ {wpm} WPM, {tone:g} Hz, "
        f"{len(samples)/48000:.1f}s, noise {noise:g}")

    eng = fldigi.Fldigi(home=cfg["_home"], input_device=cfg["input_device"],
                        tone=tone, wpm=wpm, bandwidth=cfg["bandwidth_hz"],
                        matched_filter=cfg["matched_filter"],
                        port=cfg["xmlrpc_port"], binary=cfg.get("fldigi_binary"),
                        log=log)
    eng.start()
    try:
        start = os.path.getsize(eng.textout) if os.path.exists(eng.textout) else 0
        subprocess.Popen(player, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + len(samples) / 48000 + 20
        text = ""
        while time.time() < deadline:
            time.sleep(0.5)
            try:
                with open(eng.textout, "rb") as f:
                    f.seek(start)
                    text = f.read().decode("utf-8", errors="replace")
            except OSError:
                pass
            if _norm(message) and _norm(message) in _norm(text):
                break
        log(f"  decoded: {_norm(text)!r}")
        ratio = similarity(message, text)
        check("decoded text matches", ratio >= 0.85, f"similarity {ratio:.2f}")
        check("callsign copied", "N0CALL" in _norm(text))
    finally:
        if not keep:
            eng.stop()
            pulse.stop_monitor(os.path.join(cfg["_home"], "run", "pulse-modules.json"))
    ok = all(r[1] for r in report)
    return report, ok
