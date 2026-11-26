"""Tests for shorthand expansion in the CW log.

Mirrors the js8call-logger glossary tests: the rendering must never destroy the
original token, must flag uncertain readings, and must leave callsigns alone.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from morse_logger import translate  # noqa: E402


class TranslateTest(unittest.TestCase):
    def test_protocol_words_expand(self):
        text, n = translate.translate("CQ CQ DE N0CALL K")
        self.assertEqual(text, "all stations[CQ] CQ from[DE] N0CALL go ahead[K]")
        self.assertEqual(n, 3)

    def test_repeated_token_expanded_once(self):
        text, n = translate.translate("CQ CQ CQ")
        self.assertEqual(text, "all stations[CQ] CQ CQ")
        self.assertEqual(n, 1)

    def test_callsigns_are_untouched(self):
        text, n = translate.translate("DE N0CALL")
        self.assertIn("N0CALL", text)
        self.assertEqual(n, 1)              # only DE

    def test_unknown_words_are_untouched(self):
        text, n = translate.translate("HELLO THERE")
        self.assertEqual(text, "HELLO THERE")
        self.assertEqual(n, 0)

    def test_q_signals_and_classic_shorthand(self):
        text, _n = translate.translate("TNX FER QSO 73 GL BURO")
        for want in ("thanks[TNX]", "for[FER]", "contact[QSO]",
                     "best regards[73]", "good luck[GL]", "bureau[BURO]"):
            self.assertIn(want, text)

    def test_replace_mode_has_no_brackets(self):
        self.assertEqual(translate.translate("TNX OM", mode="replace")[0],
                         "thanks op")

    def test_off_mode_returns_the_text(self):
        text, n = translate.translate("TNX OM", mode="off")
        self.assertEqual((text, n), ("TNX OM", 0))

    def test_uncertain_reading_is_flagged(self):
        text, _n = translate.translate("R 599")
        self.assertIn("received[R?]", text)

    def test_confident_reading_has_no_flag(self):
        text, _n = translate.translate("CQ R")
        self.assertIn("received[R]", text)

    def test_split_callsign_k_is_left_alone(self):
        text, n = translate.translate("K 4EIE")
        self.assertEqual(text, "K 4EIE")
        self.assertEqual(n, 0)

    def test_w_after_a_number_is_watts(self):
        self.assertIn("watts[W]", translate.translate("100 W")[0])
        self.assertIn("with[W]", translate.translate("QRV W YOU")[0])

    def test_multiplier_x_between_numbers_is_left_alone(self):
        self.assertEqual(translate.translate("2 X 3")[0], "2 X 3")

    def test_x_at_the_end_is_bye(self):
        self.assertIn("bye[X]", translate.translate("CUL X")[0])

    def test_prosigns(self):
        self.assertIn("sign off[<SK>]", translate.translate("<SK>")[0])

    def test_trailing_punctuation_is_kept(self):
        text, _n = translate.translate("TNX!")
        self.assertEqual(text, "thanks[TNX]!")

    def test_units_keep_expansion_attached(self):
        units, n = translate.translate_units("TNX OM 73")
        self.assertEqual(units, ["thanks[TNX]", "op[OM]", "best regards[73]"])
        self.assertEqual(n, 3)

    def test_low_coherence_skips_expansion(self):
        text, n = translate.translate("TNX OM", min_coherence=45)
        self.assertEqual((text, n), ("TNX OM", 0))


class CoherenceTest(unittest.TestCase):
    def test_garbage_scores_zero(self):
        self.assertEqual(translate.coherence("hq tz bv kw qx zz"), 0.0)

    def test_plain_calls(self):
        self.assertEqual(translate.coherence("CQ CQ DE N0CALL"), 1.0)

    def test_empty_is_zero(self):
        self.assertEqual(translate.coherence(""), 0.0)

    def test_four_letter_runs_are_not_evidence(self):
        self.assertLess(translate.coherence("SANG DRIE ITAC"), 0.5)

    def test_legend_mentions_both_notations(self):
        legend = translate.legend()
        self.assertIn("word[TOKEN]", legend)
        self.assertIn("TOKEN?", legend)


if __name__ == "__main__":
    unittest.main()
