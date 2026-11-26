"""Tests for the selftest's verdict arithmetic (the audio chain needs a decoder)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from morse_logger import selftest  # noqa: E402


class NormaliseTest(unittest.TestCase):
    def test_punctuation_and_case_are_dropped(self):
        self.assertEqual(selftest._norm("CQ, CQ de N0CALL!"), "CQ CQ DE N0CALL")

    def test_empty(self):
        self.assertEqual(selftest._norm(""), "")


class SimilarityTest(unittest.TestCase):
    def test_identical_is_one(self):
        self.assertEqual(selftest.similarity("CQ CQ DE N0CALL TEST",
                                             "cq cq de n0call test"), 1.0)

    def test_a_partial_copy_scores_between(self):
        got = selftest.similarity("CQ CQ CQ DE N0CALL TEST K",
                                  "CQ CQ DE N0CALL TES")
        self.assertGreater(got, 0.5)
        self.assertLess(got, 1.0)

    def test_garbage_scores_low(self):
        self.assertLess(selftest.similarity("CQ CQ DE N0CALL", "HQ TZ BV"), 0.3)

    def test_empty_expectation_is_zero(self):
        self.assertEqual(selftest.similarity("", "anything"), 0.0)


if __name__ == "__main__":
    unittest.main()
