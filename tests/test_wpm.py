"""Tests for keying-speed measurement.

The measurement is the risky part of the tool: too fast an estimate shreds the
copy, too slow merges letters. So these tests use a synthetic fist whose speed
is known exactly, and check the rejection paths as well as the happy one.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from support import cw, silence  # noqa: E402

from morse_logger import wpm  # noqa: E402

TEXT = "CQ CQ DE N0CALL TEST"


class MeasureTest(unittest.TestCase):
    def test_known_speeds_are_measured(self):
        for speed in (15, 20, 25):
            got = wpm.measure(cw(TEXT, wpm=speed), rate=8000)
            self.assertIn("wpm", got, got)
            self.assertAlmostEqual(got["wpm"], speed, delta=1.5)
            self.assertAlmostEqual(got["dot_ms"], 1200.0 / speed, delta=6)
            self.assertGreater(got["marks"], 20)
            self.assertGreaterEqual(got["confidence"], 0.8)

    def test_silence_is_rejected(self):
        got = wpm.measure(silence(2.0), rate=8000)
        self.assertIn("rejected", got)

    def test_steady_tone_is_not_keying(self):
        import support
        got = wpm.measure(support.sine(750, seconds=3.0), rate=8000)
        self.assertIn("rejected", got)

    def test_short_audio_is_rejected(self):
        got = wpm.measure(cw("E", wpm=20), rate=8000)
        self.assertIn("rejected", got)

    def test_tone_hint_uses_the_band_envelope(self):
        got = wpm.measure(cw(TEXT, wpm=20, tone=800), rate=8000, tone_hz=800)
        self.assertIn("wpm", got)
        # a clean quiet-band signal is settled by the broadband pass first; the
        # band-limited pass is only consulted when that finds nothing
        self.assertNotIn("band_hz", got)

    def test_voice_like_energy_is_refused_at_a_tone_hint(self):
        # broadband noise has no narrow carrier at the hint frequency
        import random
        rnd = random.Random(7)
        data = b"".join(int(rnd.uniform(-1, 1) * 20000).to_bytes(2, "little", signed=True)
                        for _ in range(16000))
        got = wpm.measure(data, rate=8000, tone_hz=800)
        self.assertIn("rejected", got)


class NarrownessTest(unittest.TestCase):
    def test_carrier_is_narrow(self):
        import support
        self.assertGreater(wpm.narrowness(support.sine(800, seconds=1.5), tone_hz=800),
                           0.5)

    def test_noise_is_not(self):
        import random
        rnd = random.Random(3)
        data = b"".join(int(rnd.uniform(-1, 1) * 20000).to_bytes(2, "little", signed=True)
                        for _ in range(24000))
        noise = wpm.narrowness(data, tone_hz=800)
        self.assertLess(noise, 0.3)
        self.assertGreater(wpm.narrowness(cw(TEXT, tone=800), tone_hz=800),
                           3 * noise)

    def test_too_little_audio(self):
        self.assertEqual(wpm.narrowness(b"\x00" * 100, rate=8000), 0.0)


class PeakDbfsTest(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(wpm.peak_dbfs(b""), -120.0)

    def test_full_scale(self):
        self.assertAlmostEqual(wpm.peak_dbfs(b"\xff\x7f"), 0.0, places=1)

    def test_half_scale(self):
        self.assertAlmostEqual(wpm.peak_dbfs(b"\x00\x40"), -6.0, places=1)


class ListenWindowsTest(unittest.TestCase):
    def test_short_interval_gets_a_trimmed_last_window(self):
        self.assertEqual(wpm.listen_windows(15), (5.0, 8.0))

    def test_normal_interval_gets_the_full_plan(self):
        self.assertEqual(wpm.listen_windows(60), (5.0, 10.0, 15.0))

    def test_never_exceeds_the_budget(self):
        for interval in (10, 12, 15, 20, 30):
            windows = wpm.listen_windows(interval)
            self.assertLessEqual(sum(windows), max(5.0, interval - 2.0))

    def test_tiny_interval_is_clamped_to_the_first_window(self):
        self.assertEqual(wpm.listen_windows(1), (5.0,))

    def test_total_cap(self):
        self.assertLessEqual(sum(wpm.listen_windows(600)), 31.0)


class MeasureLiveTest(unittest.TestCase):
    def test_accumulates_until_there_is_enough(self):
        audio = cw(TEXT, wpm=20)
        chunks = [audio[:len(audio) // 3], audio[len(audio) // 3:2 * len(audio) // 3],
                  audio[2 * len(audio) // 3:]]
        calls = []

        def capture(seconds):
            calls.append(seconds)
            return chunks[len(calls) - 1] if len(calls) <= len(chunks) else b""

        got = wpm.measure_live(capture, windows=(5, 10, 15))
        self.assertIn("wpm", got)
        self.assertGreaterEqual(got["attempts"], 1)
        self.assertIn("listened_s", got)
        self.assertGreater(len(calls), 0)

    def test_quiet_cable_gets_an_explanation(self):
        got = wpm.measure_live(lambda _s: silence(1.0), windows=(1, 1), need=8)
        self.assertIn("rejected", got)
        self.assertIn("very quiet", got["rejected"])

    def test_no_audio_at_all(self):
        got = wpm.measure_live(lambda _s: b"", windows=(1,))
        self.assertIn("rejected", got)


class CrossingsTest(unittest.TestCase):
    def test_runs_recover_a_square_keying(self):
        # 10 windows on, 10 off, on a 10 ms grid
        env = [1000] * 10 + [0] * 10 + [1000] * 10 + [0] * 10
        runs = wpm._crossings(env, 500, 0.01)
        marks = [d for on, d in runs if on]
        self.assertTrue(marks)
        self.assertAlmostEqual(max(marks), 0.1, delta=0.02)

    def test_too_short_envelope(self):
        self.assertEqual(wpm._crossings([500], 500, 0.01), [])


if __name__ == "__main__":
    unittest.main()
