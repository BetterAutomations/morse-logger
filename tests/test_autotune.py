"""Tests for tone detection: which frequency the CW note sits on."""

import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from support import cw, sine, silence  # noqa: E402

from morse_logger import autotune  # noqa: E402


class GoertzelTest(unittest.TestCase):
    def test_peak_at_the_input_frequency(self):
        samples = [0.3 * __import__("math").sin(2 * 3.141592653589793 * 800 * i / 8000)
                   for i in range(800)]
        at_800 = autotune._goertzel(samples, 8000, 800)
        at_400 = autotune._goertzel(samples, 8000, 400)
        self.assertGreater(at_800, at_400 * 100)

    def test_empty(self):
        self.assertEqual(autotune._goertzel([], 8000, 800), 0.0)


class PcmTest(unittest.TestCase):
    def test_scaling(self):
        self.assertEqual(autotune.pcm_to_floats(struct.pack("<3h", 0, 16384, -32768)),
                         [0.0, 0.5, -1.0])

    def test_odd_byte_is_ignored(self):
        self.assertEqual(len(autotune.pcm_to_floats(b"\x00\x40\x00")), 1)

    def test_level(self):
        self.assertEqual(autotune.level(b""), 0.0)
        self.assertGreater(autotune.level(sine(800)), 1000)


class SpectrumTest(unittest.TestCase):
    def test_spectrum_covers_the_band(self):
        spec = autotune.spectrum(autotune.pcm_to_floats(sine(800)), rate=8000)
        self.assertEqual(spec[0][0], autotune.LOW_HZ)
        self.assertLessEqual(spec[-1][0], autotune.HIGH_HZ)

    def test_scan_is_sorted_by_energy(self):
        ranked = autotune.scan(sine(800))
        self.assertGreater(len(ranked), 10)
        self.assertEqual(ranked, sorted(ranked, key=lambda t: t[1], reverse=True))
        best = ranked[0][0]
        self.assertLess(abs(best - 800), 30)

    def test_spectrum_top_shape(self):
        top = autotune.spectrum_top(sine(800), n=3)
        self.assertEqual(len(top), 3)
        self.assertEqual(set(top[0]), {"hz", "energy"})


class PickTest(unittest.TestCase):
    def test_finds_a_keyed_tone(self):
        got = autotune.pick(cw("CQ CQ DE N0CALL", tone=750.0))
        self.assertIsNotNone(got)
        self.assertLess(abs(got["tone_hz"] - 750), 30)
        self.assertEqual(got["stability"], 1.0)
        self.assertTrue(got["usable"])
        self.assertFalse(got["clipping"])

    def test_silence_finds_nothing(self):
        self.assertIsNone(autotune.pick(silence(1.0)))

    def test_too_short_audio(self):
        self.assertIsNone(autotune.pick(sine(800, seconds=0.1)))

    def test_clipping_disables_usable(self):
        import support
        got = autotune.pick(support.sine(750, seconds=1.0, amp=1.0))
        self.assertIsNotNone(got)
        self.assertTrue(got["clipping"])
        self.assertFalse(got["usable"])

    def test_choose_reports_how_the_tone_was_found(self):
        got = autotune.choose(cw("CQ CQ DE N0CALL", tone=750.0))
        self.assertTrue(got["usable"])
        self.assertIn(got["how"], ("steady", "keyed"))
        self.assertLess(abs(got["tone_hz"] - 750), 30)

    def test_choose_on_silence_is_not_usable(self):
        got = autotune.choose(silence(1.0))
        self.assertFalse(got["usable"])


class KeyingCandidatesTest(unittest.TestCase):
    def test_a_good_hint_is_used(self):
        tone, info = autotune.keying_candidates(cw("CQ CQ DE N0CALL", tone=800.0),
                                                tones=[800])
        self.assertEqual(tone, 800)
        self.assertIn("wpm", info)

    def test_no_signal_finds_nothing(self):
        tone, info = autotune.keying_candidates(silence(1.0), tones=[800])
        self.assertIsNone(tone)
        self.assertIsNone(info)


if __name__ == "__main__":
    unittest.main()
