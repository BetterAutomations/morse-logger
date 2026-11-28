"""Tests for the daemon's decisions: adopt a measured speed? re-tune?

The loop itself needs fldigi and an audio cable; these are the rules that decide
what it does, and they are where a wrong guess costs a whole session.
"""

import os
import sys
import tempfile
import time
import unittest
import contextlib
import io
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from morse_logger import logger  # noqa: E402
from morse_logger import daemon  # noqa: E402
from morse_logger.daemon import Runner  # noqa: E402


def measurement(wpm, confidence=1.0):
    return {"wpm": wpm, "dot_ms": round(1200.0 / wpm, 1), "marks": 30,
            "confidence": confidence}


class RunnerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.cfg = logger.load_config(self.home)
        self.cfg.update({"wpm": 20, "tone_hz": 1500, "watchdog": True,
                         "watchdog_window": 2, "watchdog_cooldown_s": 10,
                         "speed_watchdog_change": 0.2,
                         "speed_watchdog_max_restarts": 4,
                         "min_chars": 1, "notify": False})
        self.runner = Runner(self.cfg, verbose=False)
        self.saved = mock.patch.object(logger, "save_config")
        self.saved.start()

    def tearDown(self):
        self.saved.stop()
        self.runner.journals.close()
        self.tmp.cleanup()

    def status(self, extra=None):
        with mock.patch.object(daemon.pulse, "cable_status", return_value={}):
            return self.runner.write_status(extra)


class StatusRepairAgeTest(RunnerCase):
    """A repair from six hours ago must not read like a current one."""

    def test_no_repair_means_no_age(self):
        st = self.status()
        self.assertIsNone(st["last_repair"])
        self.assertIsNone(st["last_repair_ago"])

    def test_the_age_is_published_with_the_text(self):
        self.runner.last_repair = "cable was missing - re-created it"
        self.runner._last_repair_at = time.time() - 600
        st = self.status()
        self.assertEqual(st["last_repair"], "cable was missing - re-created it")
        self.assertGreaterEqual(st["last_repair_ago"], 599)
        self.assertLess(st["last_repair_ago"], 700)


class AdoptWpmTest(RunnerCase):
    def test_a_small_difference_is_ignored(self):
        self.assertFalse(self.runner.adopt_wpm(measurement(21), "test", restart=False))
        self.assertEqual(self.runner.cfg["wpm"], 20)

    def test_a_real_difference_is_adopted(self):
        self.assertTrue(self.runner.adopt_wpm(measurement(30), "test", restart=False))
        self.assertEqual(self.runner.cfg["wpm"], 30)
        logger.save_config.assert_called()

    def test_measurement_is_rounded(self):
        self.runner.adopt_wpm(measurement(27.4), "test", restart=False)
        self.assertEqual(self.runner.cfg["wpm"], 27)

    def test_implausible_speed_is_refused(self):
        for speed in (2, 60, 100):
            self.assertFalse(self.runner.adopt_wpm(measurement(speed), "test",
                                                   restart=False))
        self.assertEqual(self.runner.cfg["wpm"], 20)

    def test_restart_is_requested_for_the_watchdog(self):
        with mock.patch.object(self.runner, "restart_engine") as restart:
            self.assertTrue(self.runner.adopt_wpm(measurement(30), "test", restart=True))
        restart.assert_called_once_with(keep_tone=True)
        self.assertEqual(self.runner.speed_restarts, 1)
        self.assertEqual(self.runner.repairs, 1)

    def test_startup_does_not_restart(self):
        with mock.patch.object(self.runner, "restart_engine") as restart:
            self.runner.adopt_wpm(measurement(30), "startup", restart=False)
        restart.assert_not_called()


class RetuneTest(RunnerCase):
    def test_needs_a_full_window_of_bad_lines(self):
        with mock.patch.object(self.runner, "autotune", return_value={"usable": True}) as auto:
            self.runner._maybe_retune(10, 80, 100.0)          # first of two
            auto.assert_not_called()
            self.runner._maybe_retune(10, 80, 101.0)          # window full
            auto.assert_called_once()
        self.assertEqual(self.runner.retunes, 1)

    def test_good_quality_blocks_the_retune(self):
        with mock.patch.object(self.runner, "autotune") as auto:
            for i in range(5):
                self.runner._maybe_retune(10, 10, 100.0 + i)   # weak tone
            auto.assert_not_called()
        self.assertEqual(self.runner.retunes, 0)

    def test_readable_text_blocks_the_retune(self):
        with mock.patch.object(self.runner, "autotune") as auto:
            self.runner._maybe_retune(90, 80, 100.0)
            self.runner._maybe_retune(90, 80, 101.0)
            auto.assert_not_called()

    def test_cooldown_blocks_a_second_retune(self):
        with mock.patch.object(self.runner, "autotune", return_value={"usable": True}):
            self.runner._maybe_retune(10, 80, 100.0)
            self.runner._maybe_retune(10, 80, 103.0)          # retune #1
            self.runner._maybe_retune(10, 80, 104.0)
            self.runner._maybe_retune(10, 80, 105.0)          # inside cooldown
        self.assertEqual(self.runner.retunes, 1)

    def test_watchdog_off_does_nothing(self):
        self.runner.cfg["watchdog"] = False
        with mock.patch.object(self.runner, "autotune") as auto:
            self.runner._maybe_retune(0, 0, 1.0)
            self.runner._maybe_retune(0, 0, 2.0)
        auto.assert_not_called()


class StatusTest(RunnerCase):
    def test_status_carries_the_facts_the_cli_prints(self):
        self.runner.lines = 7
        self.runner.last = "2026-11-26 12:00:00  [ ... ]  CQ"
        st = self.status({"state": "running"})
        self.assertEqual(st["lines"], 7)
        self.assertEqual(st["state"], "running")
        self.assertIn("fldigi", st)
        self.assertIn("log_dir", st)
        self.assertEqual(st["input_device"], self.cfg["input_device"])
        self.assertEqual(st["wpm"], 20)


class EmitTest(RunnerCase):
    def test_too_short_text_is_dropped(self):
        self.runner.cfg["min_chars"] = 4
        self.runner.eng = None
        self.runner.emit({"text": "OK", "started": 0}, now=1.0)
        self.assertEqual(self.runner.lines, 0)

    def test_a_line_is_journalled(self):
        self.runner.cfg["translate"] = "gloss"
        self.runner.eng = None
        with mock.patch.object(self.runner, "_maybe_retune"), \
                contextlib.redirect_stdout(io.StringIO()):
            self.runner.emit({"text": "CQ CQ DE N0CALL", "started": 0}, now=1.0)
        self.assertEqual(self.runner.lines, 1)
        with open(os.path.join(self.cfg["log_dir"], "morse.jsonl")) as f:
            recs = f.read().splitlines()
        self.assertEqual(len(recs), 1)
        self.assertIn("CQ CQ DE N0CALL", recs[0])

    def test_repeats_are_counted(self):
        self.runner.cfg["repeat_window_s"] = 120
        self.runner.eng = None
        with mock.patch.object(self.runner, "_maybe_retune"), \
                contextlib.redirect_stdout(io.StringIO()):
            self.runner.emit({"text": "CQ CQ DE N0CALL", "started": 0}, now=1.0)
            self.runner.emit({"text": "CQ CQ DE N0CALL", "started": 0}, now=2.0)
        self.assertEqual(self.runner.repeat_count, 2)
        self.assertIsNotNone(self.runner.repeat_id)


class RestoreLoopsTest(RunnerCase):
    def test_saved_source_loops_are_recreated(self):
        self.runner.cfg["source_loops"] = ["alsa_input.mic",
                                           "js8call-logger_cable.monitor"]
        with mock.patch.object(daemon.pulse, "loop_source") as loop:
            self.runner._restore_source_loops()
        self.assertEqual([c.args[0] for c in loop.call_args_list],
                         ["alsa_input.mic", "js8call-logger_cable.monitor"])

    def test_a_failed_restore_does_not_stop_the_others(self):
        self.runner.cfg["source_loops"] = ["gone", "alsa_input.mic"]

        def loop(name, state_file=None):
            if name == "gone":
                raise daemon.pulse.PulseError("no such source")
            return True

        with mock.patch.object(daemon.pulse, "loop_source", side_effect=loop):
            self.runner._restore_source_loops()


class RepairTest(RunnerCase):
    def text(self):
        path = os.path.join(self.cfg["log_dir"],
                            "morse-%s.txt" % daemon.datetime.now().strftime("%Y-%m-%d"))
        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_repair_is_written_where_a_human_reads(self):
        self.runner.repair("decoder restarted")
        self.assertIn("# decoder restarted", self.text())
        self.assertEqual(self.runner.last_repair, "decoder restarted")
        self.assertEqual(self.runner.repairs, 1)

    def test_status_carries_the_last_repair(self):
        self.runner.repair("cable was gone")
        st = self.status()
        self.assertEqual(st["last_repair"], "cable was gone")
        self.assertEqual(st["repairs"], 1)

    def test_a_repeat_within_ten_minutes_is_not_resent(self):
        with mock.patch.object(daemon.time, "time", return_value=1000):
            self.runner.repair("same thing")
            self.runner.repair("same thing")
        self.assertEqual(self.runner.repairs, 1)
        self.assertEqual(self.text().count("# same thing"), 1)

    def test_the_same_text_after_ten_minutes_is_resent(self):
        with mock.patch.object(daemon.time, "time", side_effect=[1000, 2000]):
            self.runner.repair("same thing")
            self.runner.repair("same thing")
        self.assertEqual(self.runner.repairs, 2)

    def test_desktop_notification_is_best_effort(self):
        self.cfg["notify"] = True
        with mock.patch.object(daemon, "notify_desktop", return_value=True) as note:
            self.runner.repair("decoder restarted")
        note.assert_called_once_with("decoder restarted")

    def test_notification_can_be_turned_off(self):
        self.cfg["notify"] = False
        with mock.patch.object(daemon, "notify_desktop") as note:
            self.runner.repair("decoder restarted")
        note.assert_not_called()


class CableWatchTest(RunnerCase):
    def test_a_present_cable_is_left_alone(self):
        with mock.patch.object(daemon.pulse, "sink_exists", return_value=True), \
                mock.patch.object(daemon.pulse, "source_exists", return_value=True), \
                mock.patch.object(daemon.pulse, "ensure_cable") as ensure:
            self.runner.check_cable(100)
        ensure.assert_not_called()
        self.assertEqual(self.runner.repairs, 0)

    def test_a_missing_cable_is_recreated_and_reported(self):
        with mock.patch.object(daemon.pulse, "sink_exists", return_value=False), \
                mock.patch.object(daemon.pulse, "source_exists", return_value=True), \
                mock.patch.object(daemon.pulse, "ensure_cable",
                                  return_value={"sink": True}), \
                mock.patch.object(self.runner, "_restore_source_loops",
                                  return_value=1):
            self.runner.check_cable(100)
        self.assertEqual(self.runner.repairs, 2)      # cable + restored loop
        with open(os.path.join(self.cfg["log_dir"],
                               "morse-%s.txt" % daemon.datetime.now().strftime("%Y-%m-%d")),
                  encoding="utf-8") as f:
            self.assertIn("cable was gone", f.read())

    def test_the_check_runs_at_most_every_30_seconds(self):
        with mock.patch.object(daemon.pulse, "sink_exists", return_value=True), \
                mock.patch.object(daemon.pulse, "source_exists", return_value=True):
            self.runner.check_cable(100)
            self.runner.check_cable(110)             # inside the interval
            self.runner.check_cable(140)
        self.assertEqual(self.runner.last_cable_check, 140)

    def test_cable_watch_can_be_turned_off(self):
        self.runner.cfg["cable_watch"] = False
        with mock.patch.object(daemon.pulse, "sink_exists", return_value=False), \
                mock.patch.object(daemon.pulse, "ensure_cable") as ensure:
            self.runner.check_cable(100)
        ensure.assert_not_called()


class LevelWatchTest(RunnerCase):
    def test_off_by_default(self):
        with mock.patch.object(daemon.pulse, "capture_samples") as capture:
            self.runner.check_level(1000)
        capture.assert_not_called()

    def test_a_drifted_level_is_trimmed(self):
        self.runner.cfg.update({"level_auto": True, "level_target_db": -14.0,
                                "level_max_step_db": 6.0,
                                "level_auto_interval_s": 20})
        with mock.patch.object(daemon.pulse, "source_exists", return_value=True), \
                mock.patch.object(daemon.pulse, "capture_samples",
                                  return_value=b"\x00\x40"), \
                mock.patch.object(daemon.pulse, "peak_dbfs", return_value=-2.0), \
                mock.patch.object(daemon.pulse, "source_volume",
                                  return_value=100.0), \
                mock.patch.object(daemon.pulse, "set_source_volume",
                                  return_value=True) as setv, \
                mock.patch.object(self.runner, "say"):
            self.runner.check_level(1000)
        setv.assert_called_once()
        self.assertEqual(self.runner.level_dbfs, -2.0)

    def test_a_good_level_is_left_alone(self):
        self.runner.cfg.update({"level_auto": True, "level_auto_interval_s": 20})
        with mock.patch.object(daemon.pulse, "source_exists", return_value=True), \
                mock.patch.object(daemon.pulse, "capture_samples",
                                  return_value=b"\x00\x40"), \
                mock.patch.object(daemon.pulse, "peak_dbfs", return_value=-14.0), \
                mock.patch.object(daemon.pulse, "source_volume",
                                  return_value=80.0), \
                mock.patch.object(daemon.pulse, "set_source_volume") as setv:
            self.runner.check_level(1000)
        setv.assert_not_called()

    def test_the_check_is_rate_limited(self):
        self.runner.cfg.update({"level_auto": True, "level_auto_interval_s": 20})
        with mock.patch.object(daemon.pulse, "source_exists", return_value=True), \
                mock.patch.object(daemon.pulse, "capture_samples",
                                  return_value=b"\x00\x40") as capture, \
                mock.patch.object(daemon.pulse, "peak_dbfs", return_value=-14.0), \
                mock.patch.object(daemon.pulse, "source_volume",
                                  return_value=80.0):
            self.runner.check_level(1000)
            self.runner.check_level(1010)
        self.assertEqual(capture.call_count, 1)

    def test_an_empty_sdr_band_is_not_trimmed(self):
        # rtl_fm's noise floor: raising the capture volume would turn the gain
        # up on nothing and change no decode
        self.runner.cfg.update({"level_auto": True, "level_auto_interval_s": 20})
        with mock.patch.object(daemon.feed, "sdr_running", return_value=True), \
                mock.patch.object(daemon.pulse, "source_exists", return_value=True), \
                mock.patch.object(daemon.pulse, "capture_samples",
                                  return_value=b"\x01\x01"), \
                mock.patch.object(daemon.pulse, "peak_dbfs", return_value=-40.5), \
                mock.patch.object(daemon.pulse, "source_volume", return_value=100.0), \
                mock.patch.object(daemon.pulse, "set_source_volume") as setv:
            self.runner.check_level(1000)
        setv.assert_not_called()
        self.assertEqual(self.runner.level_dbfs, -40.5)

    def test_a_quiet_application_is_still_trimmed(self):
        self.runner.cfg.update({"level_auto": True, "level_auto_interval_s": 20})
        with mock.patch.object(daemon.feed, "sdr_running", return_value=False), \
                mock.patch.object(daemon.pulse, "source_exists", return_value=True), \
                mock.patch.object(daemon.pulse, "capture_samples",
                                  return_value=b"\x01\x01"), \
                mock.patch.object(daemon.pulse, "peak_dbfs", return_value=-40.5), \
                mock.patch.object(daemon.pulse, "source_volume", return_value=50.0), \
                mock.patch.object(daemon.pulse, "set_source_volume",
                                  return_value=True) as setv, \
                mock.patch.object(self.runner, "say"):
            self.runner.check_level(1000)
        setv.assert_called_once()


class RouteFlapTest(RunnerCase):
    """Another logger moving the same stream back must not cause a tug of war."""

    def setUp(self):
        super().setUp()
        self.runner.cfg.update({"route_watch": True, "route_apps": ["chrome"]})

    def watch(self, now, moved=(("1", "chrome"),)):
        with mock.patch.object(daemon.time, "time", return_value=now), \
                mock.patch.object(daemon.pulse, "route_new_streams",
                                  return_value=(list(moved), {"1"})) as route:
            self.runner._route_watch()
        return route

    def test_one_move_does_not_warn(self):
        with mock.patch.object(self.runner, "say") as say:
            self.watch(1000)
        self.assertNotIn("chrome", self.runner._warned)
        say.assert_called_once()

    def test_three_moves_in_a_minute_back_off_for_five(self):
        with mock.patch.object(self.runner, "say") as say:
            self.watch(1000)
            self.watch(1010)
            self.watch(1020)
        self.assertIn("chrome", self.runner._warned)
        self.assertEqual(self.runner._backoff["chrome"], 1320)
        self.assertTrue(any("another program is routing it too" in c.args[0]
                            for c in say.call_args_list))

    def test_backoff_suppresses_the_move(self):
        self.runner._backoff["chrome"] = 2000
        route = self.watch(1999)
        route.assert_not_called()

    def test_backoff_expires(self):
        self.runner._backoff["chrome"] = 2000
        route = self.watch(2001)
        route.assert_called_once()

    def test_a_quiet_minute_resets_the_history(self):
        with mock.patch.object(self.runner, "say"):
            self.watch(1000)
            self.watch(1010)
            self.watch(1100)          # more than 60 s after the first two
        self.assertNotIn("chrome", self.runner._warned)
        self.assertEqual(len(self.runner._moves["chrome"]), 1)


if __name__ == "__main__":
    unittest.main()
