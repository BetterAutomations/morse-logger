"""Tests for the synthetic CW generator used by selftest."""

import os
import struct
import sys
import tempfile
import unittest
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from morse_logger import morse  # noqa: E402


class TimingTest(unittest.TestCase):
    def test_dot_and_dash_lengths(self):
        segs, dot = morse.text_to_timing("ET", wpm=20)
        self.assertAlmostEqual(dot, 1.2 / 20)
        self.assertEqual(len(segs), 2)
        self.assertAlmostEqual(segs[0][0], dot)          # E is one dot
        self.assertAlmostEqual(segs[1][0], 3 * dot)      # T is one dash

    def test_inter_char_gap_is_one_dot(self):
        segs, dot = morse.text_to_timing("A", wpm=20)    # .-
        self.assertAlmostEqual(segs[0][1], dot)

    def test_word_gap_is_seven_dots(self):
        segs, dot = morse.text_to_timing("E E", wpm=20)
        self.assertAlmostEqual(segs[0][1], 7 * dot)

    def test_unknown_characters_are_skipped(self):
        segs, _dot = morse.text_to_timing("E#E", wpm=20)
        self.assertEqual(len(segs), 2)

    def test_case_insensitive(self):
        self.assertEqual(morse.text_to_timing("cq")[0], morse.text_to_timing("CQ")[0])


class SynthTest(unittest.TestCase):
    def test_synth_has_lead_and_tail_silence(self):
        samples = morse.synth("E", wpm=20, rate=8000)
        self.assertGreater(len(samples), int(8000 * 0.6))
        self.assertEqual(samples[0], 0.0)
        self.assertEqual(samples[-1], 0.0)

    def test_amplitude_is_respected(self):
        samples = morse.synth("E", wpm=20, rate=8000, amp=0.2)
        self.assertLessEqual(max(abs(s) for s in samples), 0.2 + 1e-9)

    def test_noise_is_reproducible(self):
        a = morse.synth("EE", wpm=20, rate=8000, noise=0.01)
        b = morse.synth("EE", wpm=20, rate=8000, noise=0.01)
        self.assertEqual(a, b)


class WavTest(unittest.TestCase):
    def test_write_wav_is_mono_16_bit_at_the_given_rate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.wav")
            morse.write_wav(path, morse.synth("CQ", wpm=20, rate=8000), rate=8000)
            with wave.open(path, "rb") as w:
                self.assertEqual(w.getnchannels(), 1)
                self.assertEqual(w.getsampwidth(), 2)
                self.assertEqual(w.getframerate(), 8000)
                self.assertGreater(w.getnframes(), 0)

    def test_samples_are_clamped_not_wrapped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.wav")
            morse.write_wav(path, [2.0, -2.0], rate=8000)
            with wave.open(path, "rb") as w:
                got = struct.unpack("<2h", w.readframes(2))
            self.assertEqual(got, (32767, -32767))


if __name__ == "__main__":
    unittest.main()
