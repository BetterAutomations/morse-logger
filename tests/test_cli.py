"""Tests for the CLI helpers: the follow loop, filters and argument parsing."""

import argparse
import os
import queue
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from morse_logger import cli  # noqa: E402


class UptimeTest(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(cli.fmt_uptime(0), "0m00s")
        self.assertEqual(cli.fmt_uptime(90), "1m30s")
        self.assertEqual(cli.fmt_uptime(3600), "1h00m")
        self.assertEqual(cli.fmt_uptime(3600 + 61 * 60), "2h01m")


class ParseFreqTest(unittest.TestCase):
    def test_mhz(self):
        self.assertEqual(cli.parse_freq("7.03"), 7030000)

    def test_khz(self):
        self.assertEqual(cli.parse_freq("7030"), 7030000)

    def test_hz(self):
        self.assertEqual(cli.parse_freq("7030000"), 7030000)


class PctArgTest(unittest.TestCase):
    def test_plain_percent(self):
        self.assertEqual(cli.pct_arg(45, "min-coherence"), 45)

    def test_fraction_is_scaled(self):
        self.assertEqual(cli.pct_arg(0.45, "min-coherence"), 45)

    def test_none_passes_through(self):
        self.assertIsNone(cli.pct_arg(None, "min-quality"))

    def test_above_100_is_refused(self):
        with self.assertRaises(SystemExit):
            cli.pct_arg(150, "min-quality")


def args(**kw):
    base = dict(min_quality=None, min_coherence=None, min_chars=None,
                hide_filtered=False)
    base.update(kw)
    return argparse.Namespace(**base)


class RecPassesTest(unittest.TestCase):
    def test_no_filters_accepts_everything(self):
        self.assertTrue(cli._rec_passes({"text": "hi"}, args()))

    def test_quality_floor(self):
        self.assertFalse(cli._rec_passes({"text": "hi", "quality": 10},
                                         args(min_quality=40)))
        self.assertTrue(cli._rec_passes({"text": "hi", "quality": 40},
                                        args(min_quality=40)))

    def test_missing_quality_counts_as_zero(self):
        self.assertFalse(cli._rec_passes({"text": "hi"}, args(min_quality=1)))

    def test_coherence_floor_uses_the_stored_value(self):
        self.assertFalse(cli._rec_passes({"text": "CQ", "coherence": 20},
                                         args(min_coherence=45)))
        self.assertTrue(cli._rec_passes({"text": "CQ", "coherence": 45},
                                        args(min_coherence=45)))

    def test_old_records_get_coherence_computed(self):
        rec = {"text": "CQ CQ DE N0CALL"}          # predates the field
        self.assertTrue(cli._rec_passes(rec, args(min_coherence=45)))
        self.assertTrue(rec.get("_coh_computed"))
        self.assertFalse(cli._rec_passes({"text": "hq tz bv"},
                                         args(min_coherence=45)))

    def test_min_chars(self):
        self.assertFalse(cli._rec_passes({"text": "OK"}, args(min_chars=3)))
        self.assertTrue(cli._rec_passes({"text": "OK!"}, args(min_chars=3)))

    def test_hide_filtered(self):
        self.assertFalse(cli._rec_passes({"text": "x", "filtered": 1},
                                         args(hide_filtered=True)))
        self.assertTrue(cli._rec_passes({"text": "x"}, args(hide_filtered=True)))


class TailPathTest(unittest.TestCase):
    def test_named_day(self):
        fn = cli._tail_path("/logs", "2026-11-26")
        self.assertEqual(fn(), os.path.join("/logs", "morse-2026-11-26.txt"))

    def test_today_uses_the_current_date(self):
        fn = cli._tail_path("/logs")
        self.assertEqual(os.path.basename(fn()),
                         "morse-%s.txt" % time.strftime("%Y-%m-%d"))


class FollowLinesTest(unittest.TestCase):
    """The four ways a naive tail -f goes quiet forever."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.q = queue.Queue()
        self.stop = threading.Event()
        self.thread = None
        self.path = os.path.join(self.dir, "log.txt")

    def tearDown(self):
        cli._STOP["now"] = True
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=2)
        cli._STOP["now"] = False
        self.tmp.cleanup()

    def start(self, path_fn, start_at_end=False):
        cli._STOP["now"] = False

        def run():
            try:
                for line in cli._follow_lines(path_fn, poll=0.02,
                                              start_at_end=start_at_end):
                    self.q.put(line)
            finally:
                self.q.put(None)

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def next_line(self, timeout=2.0):
        return self.q.get(timeout=timeout)

    def write(self, text, mode="a", path=None):
        with open(path or self.path, mode) as f:
            f.write(text)

    def test_appended_complete_line(self):
        self.write("one\n", mode="w")
        self.start(lambda: self.path)
        self.assertEqual(self.next_line(), "one")

    def test_half_written_line_is_held_until_complete(self):
        self.write("one\npar", mode="w")
        self.start(lambda: self.path)
        self.assertEqual(self.next_line(), "one")
        self.write("tial\n")
        self.assertEqual(self.next_line(), "partial")

    def test_truncation_restarts(self):
        self.write("first line\n", mode="w")
        self.start(lambda: self.path)
        self.assertEqual(self.next_line(), "first line")
        self.write("new\n", mode="w")             # smaller file
        self.assertEqual(self.next_line(), "new")

    def test_path_switch_at_midnight(self):
        day1 = os.path.join(self.dir, "day1.txt")
        day2 = os.path.join(self.dir, "day2.txt")
        self.write("yesterday\n", mode="w", path=day1)
        current = {"path": day1}
        self.start(lambda: current["path"])
        self.assertEqual(self.next_line(), "yesterday")
        self.write("today\n", mode="w", path=day2)
        current["path"] = day2                    # date rollover
        self.assertEqual(self.next_line(), "today")

    def test_utf8_split_across_writes_is_reassembled(self):
        self.write("caf", mode="w")
        self.start(lambda: self.path)
        self.write("\u00e9\n")                    # second half of the sequence
        self.assertEqual(self.next_line(), "caf\u00e9")

    def test_file_appearing_later_is_picked_up(self):
        self.start(lambda: self.path)
        time.sleep(0.05)
        self.write("late\n", mode="w")
        self.assertEqual(self.next_line(), "late")

    def test_start_at_end_skips_existing_content(self):
        self.write("stale\n", mode="w")
        self.start(lambda: self.path, start_at_end=True)
        time.sleep(0.08)
        self.write("fresh\n")
        self.assertEqual(self.next_line(), "fresh")

    def test_blank_lines_are_not_emitted(self):
        self.write("one\n\n", mode="w")
        self.start(lambda: self.path)
        self.assertEqual(self.next_line(), "one")


if __name__ == "__main__":
    unittest.main()
