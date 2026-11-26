"""Tests for the log pipeline: the tailer, the line assembler and the journals.

The follow-loop rules (rotation, truncation, a half-written line, a path switch
at midnight) and the wrapper arithmetic are pure functions; they need no audio
and no decoder, which is what makes them worth testing here.
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from morse_logger import logger  # noqa: E402


class TailerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "textout.txt")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, text, mode="a"):
        with open(self.path, mode) as f:
            f.write(text)

    def test_reads_appended_text(self):
        self.write("one\ntwo\n", mode="w")
        t = logger.Tailer(self.path)
        self.assertEqual(t.read(), "one\ntwo\n")
        self.assertEqual(t.read(), "")
        self.write("three\n")
        self.assertEqual(t.read(), "three\n")

    def test_truncation_restarts_from_the_beginning(self):
        self.write("long line one\n", mode="w")
        t = logger.Tailer(self.path)
        t.read()
        self.write("new\n", mode="w")          # fldigi truncates on restart
        self.assertEqual(t.read(), "new\n")

    def test_replacement_by_a_new_inode_is_followed(self):
        self.write("old\n", mode="w")
        t = logger.Tailer(self.path)
        t.read()
        os.unlink(self.path)
        self.write("replacement text\n", mode="w")
        self.assertEqual(t.read(), "replacement text\n")

    def test_missing_file_is_not_an_error(self):
        t = logger.Tailer(self.path)
        self.assertEqual(t.read(), "")
        self.write("later\n", mode="w")
        self.assertEqual(t.read(), "later\n")

    def test_from_end_skips_stale_text(self):
        self.write("stale decode\n", mode="w")
        t = logger.Tailer(self.path, from_end=True)
        self.assertEqual(t.read(), "")
        self.write("fresh\n")
        self.assertEqual(t.read(), "fresh\n")

    def test_partial_multibyte_character_survives(self):
        self.write("caf", mode="w")
        t = logger.Tailer(self.path)
        first = t.read()
        self.write("\u00e9\n")                 # the rest of the UTF-8 sequence
        # the physical file is read in bytes, so the tailer can add the rest;
        # decoding happens per read, so each half may look odd but nothing is
        # lost from the stream
        self.assertEqual(first + t.read(), "caf\u00e9\n")


class AssemblerTest(unittest.TestCase):
    def make(self, **kw):
        args = dict(target_chars=500, max_chars=160, min_emit_chars=8,
                    idle_flush_s=25.0, merge_timeout_s=10.0)
        args.update(kw)
        return logger.Assembler(**args)

    def test_newlines_are_whitespace_not_line_ends(self):
        a = self.make(target_chars=20)
        a.feed("CQ CQ\r\nDE\nN0CALL", now=100)
        out = a.maybe_flush(now=200)
        self.assertEqual([r["text"] for r in out], ["CQ CQ DE N0CALL"])

    def test_max_chars_emits_immediately(self):
        a = self.make(max_chars=10, min_emit_chars=2)
        out = a.feed("A" * 25, now=1)
        self.assertEqual([r["text"] for r in out], ["A" * 10, "A" * 10])

    def test_target_chars_emits_on_schedule(self):
        a = self.make(target_chars=12)
        a.feed("CQ CQ DE", now=1)
        self.assertEqual(a.maybe_flush(now=2), [])
        a.feed(" N0CALL", now=3)
        self.assertEqual("".join(a.buf), "CQ CQ DE N0CALL")
        out = a.maybe_flush(now=4)
        self.assertEqual([r["text"] for r in out], ["CQ CQ DE N0CALL"])

    def test_idle_flush_emits_a_short_remnant(self):
        a = self.make(target_chars=500)
        a.feed("IDLE REMNANT", now=1)
        self.assertEqual(a.maybe_flush(now=10), [])
        out = a.maybe_flush(now=30)
        self.assertEqual([r["text"] for r in out], ["IDLE REMNANT"])

    def test_bt_breaks_the_line_and_keeps_the_rest(self):
        a = self.make(target_chars=500)
        out = a.feed("HELLO WORLD<BT>NEXT OVER", now=1)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["split"], "BT")
        self.assertIn("HELLO WORLD", out[0]["text"])
        self.assertEqual("".join(a.buf), "NEXT OVER")

    def test_lone_bt_is_not_a_paragraph_break(self):
        a = self.make(target_chars=500)
        out = a.feed("<BT>HI", now=1)
        self.assertEqual(out, [])
        self.assertIn("<BT>", "".join(a.buf))

    def test_short_burst_is_held_and_joined_to_the_next(self):
        a = self.make(target_chars=500, min_emit_chars=4)
        a.feed("OK", now=1)
        a.maybe_flush(now=30)                       # too short: held in carry
        self.assertEqual(a.carry, "OK")
        a.feed(" NOW", now=31)
        out = a.maybe_flush(now=70)
        self.assertEqual([r["text"] for r in out], ["OK NOW"])

    def test_carry_is_released_after_the_merge_timeout(self):
        a = self.make(target_chars=500, min_emit_chars=12, merge_timeout_s=10)
        a.feed("OK", now=1)
        a.maybe_flush(now=30)
        out = a.maybe_flush(now=45)
        self.assertEqual([r["text"] for r in out], ["OK"])

    def test_flush_all_emits_buffer_and_carry(self):
        a = self.make(target_chars=500, min_emit_chars=12)
        a.feed("OK", now=1)
        a.maybe_flush(now=30)
        a.feed(" TAIL", now=31)
        out = a.flush_all(now=32)
        self.assertEqual([r["text"] for r in out], ["OK TAIL"])

    def test_cancel_drops_everything(self):
        a = self.make()
        a.feed("CQ CQ", now=1)
        a.cancel()
        self.assertEqual(a.maybe_flush(now=100), [])
        self.assertEqual(a.flush_all(now=101), [])

    def test_whitespace_only_never_becomes_a_record(self):
        a = self.make()
        a.feed("  \r\n ", now=1)
        self.assertEqual(a.maybe_flush(now=100), [])


class MarkUncertainTest(unittest.TestCase):
    def test_lowercase_becomes_the_marker(self):
        self.assertEqual(logger.mark_uncertain("ABcD"), "AB~D")

    def test_marker_can_be_disabled(self):
        self.assertEqual(logger.mark_uncertain("ABcD", ""), "ABcD")

    def test_empty_text(self):
        self.assertEqual(logger.mark_uncertain(""), "")


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_fresh_home_gets_dirs_and_a_config_file(self):
        cfg = logger.load_config(self.home)
        for sub in ("", "logs", "run"):
            self.assertTrue(os.path.isdir(os.path.join(self.home, sub)))
        self.assertTrue(os.path.exists(os.path.join(self.home, "config.json")))
        self.assertEqual(cfg["_home"], self.home)

    def test_create_false_does_not_touch_the_filesystem(self):
        fresh = os.path.join(self.home, "absent")
        logger.load_config(fresh, create=False)
        self.assertFalse(os.path.exists(fresh))

    def test_underscore_keys_are_runtime_only(self):
        cfg = logger.load_config(self.home)
        cfg["_cpu_scale"] = 4.0
        path = logger.save_config(cfg, self.home)
        with open(path) as f:
            disk = json.load(f)
        self.assertNotIn("_cpu_scale", disk)
        self.assertNotIn("_home", disk)
        self.assertIn("tone_hz", disk)

    def test_fraction_thresholds_are_normalised_to_percent(self):
        with open(os.path.join(self.home, "config.json"), "w") as f:
            json.dump({"min_coherence": 0.45, "watchdog_coherence": 0.5}, f)
        cfg = logger.load_config(self.home)
        self.assertEqual(cfg["min_coherence"], 45.0)
        self.assertEqual(cfg["watchdog_coherence"], 50.0)

    def test_log_dir_outside_the_home_is_moved_back(self):
        with open(os.path.join(self.home, "config.json"), "w") as f:
            json.dump({"log_dir": "/nonexistent/old-logs"}, f)
        cfg = logger.load_config(self.home)
        self.assertEqual(cfg["log_dir"], os.path.join(self.home, "logs"))
        self.assertIn("_log_dir_moved", cfg)

    def test_log_dir_inside_the_home_is_kept(self):
        want = os.path.join(self.home, "elsewhere")
        with open(os.path.join(self.home, "config.json"), "w") as f:
            json.dump({"log_dir": want}, f)
        cfg = logger.load_config(self.home)
        self.assertEqual(cfg["log_dir"], want)

    def test_env_var_selects_the_home(self):
        old = os.environ.get("MORSE_LOGGER_HOME")
        os.environ["MORSE_LOGGER_HOME"] = self.home
        try:
            self.assertEqual(logger.home_dir(), self.home)
        finally:
            if old is None:
                os.environ.pop("MORSE_LOGGER_HOME", None)
            else:
                os.environ["MORSE_LOGGER_HOME"] = old


class DialTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_round_trip(self):
        logger.write_dial(7.03, self.home)
        self.assertEqual(logger.read_dial(home=self.home), 7.03)

    def test_cleared_dial_reads_as_none(self):
        logger.write_dial(None, self.home)
        self.assertIsNone(logger.read_dial(default="x", home=self.home))

    def test_unset_dial_returns_the_default(self):
        self.assertEqual(logger.read_dial(default=14.0, home=self.home), 14.0)

    def test_garbage_dial_returns_the_default(self):
        logger.write_dial(7.0, self.home)
        with open(logger.dial_path(self.home), "w") as f:
            f.write("not a number")
        self.assertEqual(logger.read_dial(default=3.5, home=self.home), 3.5)


class LockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_second_daemon_cannot_take_the_lock(self):
        # flock exclusivity is per open file description, so it has to be another
        # process: a second open() in this one would not prove anything.
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        script = (
            "import sys, time\n"
            f"sys.path.insert(0, {root!r})\n"
            "from morse_logger import logger\n"
            f"h = logger.acquire_lock({self.home!r})\n"
            "print('ok' if h else 'no', flush=True)\n"
            "time.sleep(10)\n")
        holder = subprocess.Popen([sys.executable, "-c", script],
                                  stdout=subprocess.PIPE, text=True, cwd=root)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "ok")
            with open(logger.lock_path(self.home)) as f:
                self.assertEqual(f.read().strip(), str(holder.pid))
            self.assertIsNone(logger.acquire_lock(self.home))
            with open(logger.lock_path(self.home)) as f:
                # a failed attempt must not erase the holder's pid
                self.assertEqual(f.read().strip(), str(holder.pid))
        finally:
            holder.kill()
            holder.wait()
            holder.stdout.close()

    def test_lock_can_be_taken_again_after_release(self):
        first = logger.acquire_lock(self.home)
        first.close()
        second = logger.acquire_lock(self.home)
        self.assertIsNotNone(second)
        second.close()


def rec(**kw):
    base = {"text": "CQ CQ DE N0CALL", "quality": 45.0, "coherence": 88,
            "carrier_hz": 750, "dial_mhz": 7.03, "started_utc": "2026-11-26T12:00:00Z",
            "repeat_count": 1}
    base.update(kw)
    return base


class WrapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.j = logger.Journals(self.tmp.name, max_line=150)

    def tearDown(self):
        self.j.close()
        self.tmp.cleanup()

    def test_no_physical_line_exceeds_the_cap(self):
        units = ["all stations[CQ]"] + [f"word{i}" for i in range(60)]
        lines = self.j._wrap(units, "2026-11-26 12:00:00  [  7.030MHz ]  ")
        self.assertGreater(len(lines), 1)
        for line in lines:
            self.assertLessEqual(len(line), 150, line)

    def test_continuation_lines_are_indented(self):
        units = [f"word{i}" for i in range(60)]
        lines = self.j._wrap(units, "STAMP  ")
        self.assertTrue(lines[0].startswith("STAMP  "))
        self.assertTrue(lines[1].startswith("  \u2026 "))

    def test_expansion_unit_is_not_split(self):
        units = ["x" * 5, "all stations[CQ]", "y"]
        lines = self.j._wrap(units, "",)
        joined = " ".join(lines)
        self.assertIn("all stations[CQ]", joined)

    def test_empty_units_still_yield_the_prefix(self):
        self.assertEqual(self.j._wrap([], "STAMP"), ["STAMP"])


class JournalsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.j = logger.Journals(self.dir, legend="LEGEND", max_line=150)

    def tearDown(self):
        self.j.close()
        self.tmp.cleanup()

    def txt(self, day="2026-11-26"):
        path = os.path.join(self.dir, f"morse-{day}.txt")
        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8") as f:
            return f.read()

    def jsonl(self):
        with open(os.path.join(self.dir, "morse.jsonl"), encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def test_line_shape(self):
        import datetime
        line, stored = self.j.write(
            rec(), datetime.datetime(2026, 11, 26, 12, 0, 0))
        self.assertTrue(line.startswith("2026-11-26 12:00:00  ["))
        self.assertIn("  7.030MHz", line)
        self.assertIn("q=  45.0", line)
        self.assertIn("coh= 88", line)
        self.assertIn("750Hz", line)
        self.assertTrue(line.endswith("CQ CQ DE N0CALL"))
        # the text starts in the same column as js8call-logger's line
        self.assertEqual(line.index("CQ"), logger.HEADER_WIDTH)
        self.assertIsInstance(stored["id"], int)

    def test_unknown_fields_are_dashes(self):
        import datetime
        line, _ = self.j.write(rec(dial_mhz=None, quality=None,
                                   carrier_hz=None, coherence=None),
                               datetime.datetime(2026, 11, 26, 12, 0, 0))
        self.assertIn("    ---MHz", line)
        self.assertIn("q=     -", line)
        self.assertIn("   -Hz", line)
        self.assertIn("coh=  -", line)

    def test_repeat_marker_keeps_the_width(self):
        import datetime
        line, _ = self.j.write(rec(repeat_count=12),
                               datetime.datetime(2026, 11, 26, 12, 0, 0))
        self.assertEqual(line.index("CQ"), logger.HEADER_WIDTH)
        self.assertIn("x12", line)

    def test_legend_is_written_once_per_new_day(self):
        import datetime
        when = datetime.datetime(2026, 11, 26, 12, 0, 0)
        self.j.write(rec(), when)
        self.j.write(rec(), when)
        self.assertEqual(self.txt().count("# LEGEND"), 1)

    def test_filtered_record_is_out_of_the_text_log_only(self):
        import datetime
        self.j.write(rec(text="keep me"), datetime.datetime(2026, 11, 26, 12, 0, 0))
        self.j.write(rec(text="drop me", filtered=1),
                     datetime.datetime(2026, 11, 26, 12, 0, 0))
        self.assertIn("keep me", self.txt())
        self.assertNotIn("drop me", self.txt())
        self.assertEqual([r["text"] for r in self.jsonl()], ["keep me", "drop me"])

    def test_everything_reaches_sqlite_including_optional_columns(self):
        import datetime
        self.j.write(rec(text="hi", text_xlated="hi", filtered=1),
                     datetime.datetime(2026, 11, 26, 12, 0, 0))
        db = sqlite3.connect(os.path.join(self.dir, "morse.sqlite3"))
        row = db.execute("SELECT text, text_xlated, coherence, filtered FROM captures").fetchone()
        db.close()
        self.assertEqual(row, ("hi", "hi", 88.0, 1))

    def test_day_rollover_opens_a_new_file(self):
        import datetime
        self.j.write(rec(), datetime.datetime(2026, 11, 26, 23, 59, 0))
        self.j.write(rec(), datetime.datetime(2026, 11, 27, 0, 1, 0))
        self.assertIn("CQ CQ", self.txt("2026-11-26"))
        self.assertIn("CQ CQ", self.txt("2026-11-27"))
        self.assertEqual(len(self.jsonl()), 2)

    def test_note_is_marked_so_it_cannot_be_traffic(self):
        import datetime
        line = self.j.note("cable was gone - re-created it",
                           datetime.datetime(2026, 11, 26, 12, 0, 0))
        self.assertEqual(line, "2026-11-26 12:00:00  # cable was gone - re-created it")
        self.assertIn("# cable was gone", self.txt())
        self.assertEqual(self.jsonl(), [])          # an event is not a record


if __name__ == "__main__":
    unittest.main()
