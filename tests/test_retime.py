"""Tests for Farnsworth re-timing: measure the keying, rewrite the spacing."""

import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from support import cw  # noqa: E402

from morse_logger import retime  # noqa: E402

TEXT = "CQ CQ DE N0CALL TEST"


class MeasureTest(unittest.TestCase):
    def test_standard_cw_dot_length(self):
        runs, dot = retime.measure(cw(TEXT, wpm=20, tone=750), rate=8000, tone=750)
        self.assertIsNotNone(runs)
        self.assertAlmostEqual(dot, 0.06, delta=0.012)
        self.assertTrue(any(on for on, _d in runs))

    def test_silence_finds_nothing(self):
        import support
        runs, dot = retime.measure(support.silence(1.0), rate=8000, tone=750)
        self.assertIsNone(runs)
        self.assertIsNone(dot)

    def test_steady_tone_is_not_keying(self):
        import support
        runs, dot = retime.measure(support.sine(750, seconds=2.0), rate=8000, tone=750)
        self.assertIsNone(runs)


class GapThresholdsTest(unittest.TestCase):
    def test_farnsworth_gaps_are_recognised(self):
        dot = 0.06
        # 10 ms marks, standard 60 ms element gaps, very long letter gaps
        runs = [(True, dot)] + [(True, dot) for _ in range(40)]
        runs += [(False, 0.06)] * 20 + [(False, 2.0)] * 4
        letter, word = retime.gap_thresholds(runs, dot)
        self.assertGreaterEqual(letter, 2 * dot)
        self.assertGreater(word, letter)

    def test_no_gaps_falls_back_to_standard_ratios(self):
        letter, word = retime.gap_thresholds([(True, 0.06)], 0.06)
        self.assertEqual((letter, word), (3 * 0.06, 7 * 0.06))


class RetimeTest(unittest.TestCase):
    def test_produces_pcm_and_stats(self):
        pcm, stats = retime.retime(cw(TEXT, wpm=20, tone=750), rate=8000, tone=750)
        self.assertIsNotNone(pcm)
        self.assertGreater(len(pcm), 0)
        self.assertNotIn("error", stats)
        self.assertGreater(stats["marks"], 20)
        self.assertEqual(stats["dots"] + stats["dashes"], stats["marks"])
        self.assertGreater(stats["words"], 0)
        self.assertAlmostEqual(stats["dot_ms"], 60, delta=12)

    def test_output_is_valid_s16le(self):
        pcm, stats = retime.retime(cw(TEXT, wpm=20, tone=750), rate=8000, tone=750)
        self.assertEqual(len(pcm) % 2, 0)
        samples = struct.unpack("<%dh" % (len(pcm) // 2), pcm)
        self.assertLessEqual(max(abs(s) for s in samples), 32767)

    def test_a_dot_override_is_honoured(self):
        pcm, stats = retime.retime(cw(TEXT, wpm=20, tone=750), rate=8000,
                                   tone=750, dot_s=0.08)
        self.assertEqual(stats["dot_ms"], 80.0)

    def test_no_keying_is_an_error_not_a_crash(self):
        import support
        pcm, stats = retime.retime(support.silence(1.0), rate=8000, tone=750)
        self.assertIsNone(pcm)
        self.assertIn("error", stats)


if __name__ == "__main__":
    unittest.main()
