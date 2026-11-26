"""Tests for the CLI helpers: the follow loop, filters and argument parsing."""

import argparse
import contextlib
import io
import os
import queue
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

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


def route_args(**kw):
    base = dict(all=False, index=None, copy=False, stop_copy=False,
                source=None, stop_source=False, list_sources=False,
                list=False, to=1, channels=1, no_monitor=False)
    base.update(kw)
    return argparse.Namespace(**base)


class RoutePersistenceTest(unittest.TestCase):
    """route-audio records what it did, so a restart can rebuild it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = {"_home": self.tmp.name, "monitor": True, "tap": False,
                    "source_loops": [], "route_apps": []}
        self.saved = []
        self.load = mock.patch.object(cli.logger, "load_config",
                                      return_value=self.cfg)
        self.save = mock.patch.object(
            cli.logger, "save_config",
            side_effect=lambda c, home=None: self.saved.append(dict(c)))
        self.load.start()
        self.save.start()
        self.out = mock.patch("sys.stdout", new_callable=io.StringIO)
        self.out_mock = self.out.start()

    def tearDown(self):
        self.out.stop()
        self.load.stop()
        self.save.stop()
        self.tmp.cleanup()

    def test_source_loop_is_remembered(self):
        with mock.patch.object(cli.pulse, "loop_source", return_value=True):
            self.assertEqual(cli.cmd_route(route_args(source="alsa_input.mic")), 0)
        self.assertEqual(self.saved[-1]["source_loops"], ["alsa_input.mic"])

    def test_repeated_source_loop_is_not_duplicated(self):
        self.cfg["source_loops"] = ["alsa_input.mic"]
        with mock.patch.object(cli.pulse, "loop_source", return_value=False):
            cli.cmd_route(route_args(source="alsa_input.mic"))
        self.assertEqual(self.saved[-1]["source_loops"], ["alsa_input.mic"])

    def test_stop_source_forgets_the_loops(self):
        self.cfg["source_loops"] = ["alsa_input.mic"]
        with mock.patch.object(cli.pulse, "stop_source_loops", return_value=["7"]):
            cli.cmd_route(route_args(stop_source=True))
        self.assertEqual(self.saved[-1]["source_loops"], [])

    def test_copy_is_remembered(self):
        with mock.patch.object(cli.pulse, "ensure_cable",
                               return_value={"tap": True}), \
                mock.patch.object(cli.pulse, "_tap_active", return_value=True), \
                mock.patch.object(cli.pulse, "cable_status",
                                  return_value={"monitor": False}):
            self.assertEqual(cli.cmd_route(route_args(copy=True)), 0)
        self.assertTrue(self.saved[-1]["tap"])

    def test_stop_copy_forgets_the_tap(self):
        self.cfg["tap"] = True
        with mock.patch.object(cli.pulse, "stop_tap", return_value=["9"]):
            cli.cmd_route(route_args(stop_copy=True))
        self.assertFalse(self.saved[-1]["tap"])

    def test_cable_up_no_monitor_persists(self):
        with mock.patch.object(cli.pulse, "stop_monitor", return_value=[]), \
                mock.patch.object(cli.pulse, "ensure_cable",
                                  return_value={"monitor": False, "tap": False,
                                                "sink": False,
                                                "monitor_skipped": False}), \
                mock.patch.object(cli.pulse, "cable_status",
                                  return_value={"monitor": False}):
            self.assertEqual(cli.cmd_cable(route_args(action="up",
                                                      no_monitor=True)), 0)
        self.assertFalse(self.saved[-1]["monitor"])

    def test_cable_up_with_monitor_persists(self):
        with mock.patch.object(cli.pulse, "ensure_cable",
                               return_value={"monitor": True, "tap": False,
                                             "sink": False,
                                             "monitor_skipped": False}), \
                mock.patch.object(cli.pulse, "cable_status",
                                  return_value={"monitor": True}), \
                mock.patch.object(cli.pulse, "_tap_active", return_value=False):
            self.assertEqual(cli.cmd_cable(route_args(action="up")), 0)
        self.assertTrue(self.saved[-1]["monitor"])

    def test_cable_down_forgets_everything(self):
        self.cfg.update({"monitor": True, "tap": True,
                         "source_loops": ["alsa_input.mic"]})
        with mock.patch.object(cli.pulse, "cable_down", return_value=["1"]):
            cli.cmd_cable(route_args(action="down"))
        self.assertEqual(self.saved[-1]["monitor"], False)
        self.assertEqual(self.saved[-1]["tap"], False)
        self.assertEqual(self.saved[-1]["source_loops"], [])


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


class PruneCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = {"_home": self.tmp.name,
                    "log_dir": os.path.join(self.tmp.name, "logs"),
                    "retention_days": 0}
        self.load = mock.patch.object(cli.logger, "load_config",
                                      return_value=self.cfg)
        self.load.start()
        self.out = mock.patch("sys.stdout", new_callable=io.StringIO)
        self.out_mock = self.out.start()

    def tearDown(self):
        self.out.stop()
        self.load.stop()
        self.tmp.cleanup()

    def prune(self, **kw):
        args = argparse.Namespace(days=None, dry_run=False, delete=False,
                                  force=False)
        for key, value in kw.items():
            setattr(args, key, value)
        return cli.cmd_prune(args)

    def test_retention_off_by_default(self):
        self.assertEqual(self.prune(), 2)
        self.assertIn("retention is off", self.out_mock.getvalue())

    def test_days_argument_overrides_the_config(self):
        with mock.patch.object(cli.retention, "prune",
                               return_value={"cutoff": "x", "days": 7,
                                             "files": 0, "jsonl_lines": 0,
                                             "rows": 0, "bytes": 0,
                                             "delete": False,
                                             "dry_run": False}) as prune:
            self.assertEqual(self.prune(days=7), 0)
        self.assertEqual(prune.call_args[0][2], 7)

    def test_refuses_while_the_logger_runs(self):
        with mock.patch.object(cli.logger, "lock_holder", return_value=1234):
            self.assertEqual(self.prune(days=7), 2)
        self.assertIn("the logger is running", self.out_mock.getvalue())

    def test_force_runs_while_the_logger_runs(self):
        with mock.patch.object(cli.logger, "lock_holder", return_value=1234), \
                mock.patch.object(cli.retention, "prune",
                                  return_value={"cutoff": "x", "days": 7,
                                                "files": 0, "jsonl_lines": 0,
                                                "rows": 0, "bytes": 0,
                                                "delete": False,
                                                "dry_run": True}):
            self.assertEqual(self.prune(days=7, force=True), 0)


if __name__ == "__main__":
    unittest.main()
