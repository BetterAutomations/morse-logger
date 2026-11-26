"""Tests for the pactl parsing that decides what gets moved where.

No PulseAudio/PipeWire server is needed: the parsers run on captured text, and
the module inspection is fed a stubbed `pactl list modules`.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from morse_logger import pulse  # noqa: E402

SINK_INPUTS = """Sink Input #269298
\tDriver: PipeWire
\tOwner Module: 60
\tSink: 71
\tSample Specification: float32le 1ch 48000Hz
\tapplication.name = "pw-play"
\tmedia.name = "/tmp/tone.wav"
\tCorked: no

Sink Input #172927
\tOwner Module: 52
\tSink: 71
\tapplication.name = "?"
\tmedia.name = "loopback-2167-13 output"
\tCorked: no
"""

MODULES = """Module #51
\tName: module-loopback
\tArgument: source=morse-logger_cable.monitor sink=alsa_output.analog-stereo latency_msec=60

Module #52
\tName: module-loopback
\tArgument: source=alsa_output.analog-stereo.monitor sink=morse-logger_cable latency_msec=40

Module #53
\tName: module-loopback
\tArgument: source=alsa_input.usb-mic sink=morse-logger_cable latency_msec=40

Module #60
\tName: module-null-sink
\tArgument: sink_name=morse-logger_cable rate=48000

Module #61
\tName: module-remap-source
\tArgument: master=morse-logger_cable.monitor source_name=morse-logger_cable_src
"""


class NamingTest(unittest.TestCase):
    def test_channel_names(self):
        self.assertEqual(pulse.sink_name(1), "morse-logger_cable")
        self.assertEqual(pulse.sink_name(2), "morse-logger_cable_2")
        self.assertEqual(pulse.source_name(1), "morse-logger_cable_src")
        self.assertEqual(pulse.source_name(2), "morse-logger_cable_src_2")

    def test_channel_zero_is_the_first_cable(self):
        self.assertEqual(pulse.sink_name(0), "morse-logger_cable")


class SinkInputsTest(unittest.TestCase):
    def details(self):
        with mock.patch.object(pulse, "_run",
                               return_value=mock.Mock(stdout=SINK_INPUTS.encode())), \
                mock.patch.object(pulse, "sinks", return_value=[["71", "morse-logger_cable"]]):
            return pulse.sink_input_details()

    def test_sink_index_is_resolved_to_a_name(self):
        rows = self.details()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["index"], 269298)
        self.assertEqual(rows[0]["sink"], "71")
        self.assertEqual(rows[0]["sink_name"], "morse-logger_cable")
        self.assertEqual(rows[0]["app"], "pw-play")
        self.assertEqual(rows[0]["stream"], "/tmp/tone.wav")

    def test_unknown_app_is_a_question_mark(self):
        self.assertEqual(self.details()[1]["app"], "?")

    def test_own_loopback_detected(self):
        rows = self.details()
        self.assertFalse(pulse.is_own_loopback(rows[0]))
        self.assertTrue(pulse.is_own_loopback(rows[1]))

    def test_ownership_can_come_from_the_module_id(self):
        rows = self.details()
        # module 52 is ours; a "loopback-" name owned by module 99 is not
        self.assertTrue(pulse.is_own_loopback(rows[1], own={"52"}))
        self.assertFalse(pulse.is_own_loopback(rows[1], own={"99"}))

    def test_our_modules_reads_the_state_file(self):
        with mock.patch("os.path.exists", return_value=True), \
                mock.patch("builtins.open", mock.mock_open(
                    read_data='{"modules": ["51", "52"]}')):
            self.assertEqual(pulse.our_modules("/x/pulse-modules.json"), {"51", "52"})

    def test_missing_state_file_means_no_known_modules(self):
        self.assertEqual(pulse.our_modules("/nonexistent/pulse-modules.json"), set())

    def test_blank_output(self):
        with mock.patch.object(pulse, "_run", return_value=mock.Mock(stdout=b"")), \
                mock.patch.object(pulse, "sinks", return_value=[]):
            self.assertEqual(pulse.sink_input_details(), [])


class ModuleTest(unittest.TestCase):
    def setUp(self):
        self.sink = mock.patch.object(pulse, "default_sink_name",
                                      return_value="alsa_output.analog-stereo")
        self.sink.start()

    def tearDown(self):
        self.sink.stop()

    def test_monitor_and_tap_are_distinguished(self):
        with mock.patch.object(pulse, "_module_blocks", return_value=[
                ("51", MODULES.split("Module #")[1]),
                ("52", MODULES.split("Module #")[2]),
                ("53", MODULES.split("Module #")[3])]):
            self.assertTrue(pulse._monitor_active())
            self.assertTrue(pulse._tap_active())
            self.assertTrue(pulse._is_tap_block(MODULES.split("Module #")[2]))
            self.assertFalse(pulse._is_tap_block(MODULES.split("Module #")[1]))

    def test_line_in_loop_is_not_a_tap(self):
        # A line-in (or another logger's cable monitor) feeding our cable is a
        # source loop, not the speakers tap. Matching ".monitor" as a substring
        # used to make this unremovable.
        self.assertFalse(pulse._is_tap_block(MODULES.split("Module #")[3]))

    def test_other_cables_monitor_is_a_source_loop(self):
        block = ("Module #70\n\tName: module-loopback\n\tArgument: "
                 "source=js8call-logger_cable.monitor sink=morse-logger_cable")
        self.assertFalse(pulse._is_tap_block(block))

    def test_unknown_default_sink_falls_back_to_any_monitor(self):
        with mock.patch.object(pulse, "default_sink_name", return_value=""):
            self.assertTrue(pulse._is_tap_block(MODULES.split("Module #")[2]))

    def test_stop_source_loops_keeps_the_tap_and_kills_the_line_in(self):
        unloaded = []
        with mock.patch.object(pulse, "_module_blocks", return_value=[
                ("51", MODULES.split("Module #")[1]),
                ("52", MODULES.split("Module #")[2]),
                ("53", MODULES.split("Module #")[3])]), \
                mock.patch.object(pulse, "_run",
                                  side_effect=lambda args, **kw: unloaded.append(args[2])):
            killed = pulse.stop_source_loops()
        self.assertEqual(killed, ["53"])
        self.assertEqual(unloaded, ["53"])

    def test_stop_source_loops_removes_another_cables_monitor(self):
        block = ("Module #70\n\tName: module-loopback\n\tArgument: "
                 "source=js8call-logger_cable.monitor sink=morse-logger_cable")
        with mock.patch.object(pulse, "_module_blocks", return_value=[("70", block)]), \
                mock.patch.object(pulse, "_run", return_value=mock.Mock(returncode=0)):
            self.assertEqual(pulse.stop_source_loops(), ["70"])

    def test_modules_for_channel_matches_sink_source_and_master(self):
        with mock.patch.object(pulse, "_module_blocks", return_value=[
                ("51", MODULES.split("Module #")[1]),
                ("52", MODULES.split("Module #")[2]),
                ("53", MODULES.split("Module #")[3]),
                ("60", MODULES.split("Module #")[4]),
                ("61", MODULES.split("Module #")[5])]):
            self.assertEqual(pulse.modules_for_channel(1), ["51", "52", "53", "60", "61"])

    def test_other_channels_do_not_match(self):
        with mock.patch.object(pulse, "_module_blocks", return_value=[
                ("51", MODULES.split("Module #")[1]),
                ("52", MODULES.split("Module #")[2])]):
            self.assertEqual(pulse.modules_for_channel(2), [])

    def test_pulse_error_is_raised_with_a_hint(self):
        with mock.patch("shutil.which", return_value=None):
            with self.assertRaises(pulse.PulseError) as ctx:
                pulse._run(["pactl", "info"])
            self.assertIn("pactl", str(ctx.exception))


class MonitorLoopsTest(unittest.TestCase):
    """The echo trap: the cable's own monitor reaching the speakers twice."""

    SPK = "alsa_output.analog-stereo"

    def trap(self, blocks, channels=(1,), default=None):
        return mock.patch.object(pulse, "_module_blocks", return_value=blocks), \
               mock.patch.object(pulse, "default_sink_name",
                                 return_value=self.SPK if default is None else default), \
               mock.patch.object(pulse, "cable_channels", return_value=list(channels))

    def test_our_monitor_to_the_speakers_is_found(self):
        a, b, c = self.trap([("7", "Name: module-loopback\n\tArgument: "
                                   "source=morse-logger_cable.monitor "
                                   "sink=" + self.SPK)])
        with a, b, c:
            self.assertEqual(pulse.monitor_loops_to_default(), ["7"])

    def test_a_feed_into_the_cable_is_not_the_trap(self):
        a, b, c = self.trap([("8", "Name: module-loopback\n\tArgument: "
                                   "source=js8call-logger_cable.monitor "
                                   "sink=morse-logger_cable")])
        with a, b, c:
            self.assertEqual(pulse.monitor_loops_to_default(), [])

    def test_another_programs_monitor_is_not_ours(self):
        a, b, c = self.trap([("9", "Name: module-loopback\n\tArgument: "
                                   "source=morse-logger_cable_9.monitor "
                                   "sink=" + self.SPK)])
        with a, b, c:
            self.assertEqual(pulse.monitor_loops_to_default(), [])

    def test_a_second_channel_monitor_counts(self):
        a, b, c = self.trap([("10", "Name: module-loopback\n\tArgument: "
                                    "source=morse-logger_cable_2.monitor "
                                    "sink=" + self.SPK)], channels=(1, 2))
        with a, b, c:
            self.assertEqual(pulse.monitor_loops_to_default(), ["10"])

    def test_no_default_sink_means_nothing_to_check(self):
        a, b, c = self.trap([("7", "Name: module-loopback\n\tArgument: "
                                   "source=morse-logger_cable.monitor "
                                   "sink=" + self.SPK)], default="")
        with a, b, c:
            self.assertEqual(pulse.monitor_loops_to_default(), [])


class SourceNamesTest(unittest.TestCase):
    def test_monitors_and_our_cables_are_filtered_out(self):
        rows = ("1\talsa_input.usb-mic\tmodule-alsa-card.c\n"
                "2\talsa_output.analog-stereo.monitor\tmodule-alsa-card.c\n"
                "3\tmorse-logger_cable_src\tmodule-remap-source.c\n"
                "4\tmorse-logger_cable.monitor\tmodule-null-sink.c\n")
        with mock.patch.object(pulse, "_run", return_value=mock.Mock(stdout=rows.encode())):
            self.assertEqual(pulse.source_names(), ["alsa_input.usb-mic"])

    def test_monitor_sources_lists_other_programs_outputs(self):
        rows = ("1\talsa_input.usb-mic\tmodule-alsa-card.c\n"
                "2\talsa_output.analog-stereo.monitor\tmodule-alsa-card.c\n"
                "3\tmorse-logger_cable_src\tmodule-remap-source.c\n"
                "4\tmorse-logger_cable.monitor\tmodule-null-sink.c\n"
                "5\tmorse-logger_cable_2.monitor\tmodule-null-sink.c\n"
                "6\tjs8call-logger_cable.monitor\tmodule-null-sink.c\n")
        with mock.patch.object(pulse, "_run", return_value=mock.Mock(stdout=rows.encode())):
            self.assertEqual(pulse.monitor_sources(),
                             ["alsa_output.analog-stereo.monitor",
                              "js8call-logger_cable.monitor"])

    def test_another_loggers_cable_is_a_valid_source(self):
        with mock.patch.object(pulse, "source_names", return_value=[]), \
                mock.patch.object(pulse, "monitor_sources",
                                  return_value=["js8call-logger_cable.monitor"]), \
                mock.patch.object(pulse, "_module_blocks", return_value=[]), \
                mock.patch.object(pulse, "_load_module", return_value="77") as load:
            self.assertTrue(pulse.loop_source("js8call-logger_cable.monitor"))
        self.assertIn("source=js8call-logger_cable.monitor", load.call_args[0][0])


class RouteTest(unittest.TestCase):
    def rows(self):
        return [
            {"index": 1, "sink": "0", "sink_name": "speakers",
             "app": "chrome", "stream": "WebSDR"},
            {"index": 2, "sink": "0", "sink_name": "speakers",
             "app": "?", "stream": "loopback-9-1 output"},
            {"index": 3, "sink": "9", "sink_name": "morse-logger_cable",
             "app": "chrome", "stream": "other tab"},
        ]

    def test_only_foreign_streams_are_moved(self):
        moved_calls = []

        def run(args, check=False, **_kw):
            moved_calls.append(args)
            return mock.Mock(returncode=0)

        with mock.patch.object(pulse, "sink_input_details", return_value=self.rows()), \
                mock.patch.object(pulse, "_run", side_effect=run):
            moved, skipped = pulse.route_sink_inputs()
        self.assertEqual(moved, [("1", "chrome")])
        self.assertEqual([s[2] for s in skipped],
                         ["morse-logger's own monitor loopback", "already on the cable"])
        self.assertEqual(len(moved_calls), 1)

    def test_explicit_indices_limit_the_move(self):
        with mock.patch.object(pulse, "sink_input_details", return_value=self.rows()), \
                mock.patch.object(pulse, "_run", return_value=mock.Mock(returncode=0)):
            moved, _skipped = pulse.route_sink_inputs(indices=[3])
        self.assertEqual(moved, [])

    def test_new_streams_for_recorded_apps_only(self):
        with mock.patch.object(pulse, "sink_input_details", return_value=self.rows()), \
                mock.patch.object(pulse, "_run", return_value=mock.Mock(returncode=0)):
            moved, known = pulse.route_new_streams(apps=["chrome"], known=set())
        self.assertEqual(moved, [("1", "chrome")])
        self.assertEqual(known, {"1"})


class VolumeTest(unittest.TestCase):
    def test_peak_dbfs(self):
        self.assertEqual(pulse.peak_dbfs(b""), -120.0)
        self.assertAlmostEqual(pulse.peak_dbfs(b"\xff\x7f"), 0.0, places=1)
        self.assertAlmostEqual(pulse.peak_dbfs(b"\x00\x40"), -6.0, places=1)

    def test_source_volume_is_parsed(self):
        out = b"Volume: front-left: 32768 /  50% / -18.06 dB,   front-right: 32768 /  50%\n"
        with mock.patch.object(pulse, "_run",
                               return_value=mock.Mock(returncode=0, stdout=out)):
            self.assertEqual(pulse.source_volume("src"), 50.0)

    def test_unreadable_volume(self):
        with mock.patch.object(pulse, "_run",
                               return_value=mock.Mock(returncode=0, stdout=b"nonsense")):
            self.assertIsNone(pulse.source_volume("src"))
        with mock.patch.object(pulse, "_run",
                               return_value=mock.Mock(returncode=1, stdout=b"")):
            self.assertIsNone(pulse.source_volume("src"))

    def test_set_volume_never_amplifies_past_unity(self):
        calls = []
        with mock.patch.object(pulse, "_run",
                               side_effect=lambda args, **kw: calls.append(args)
                               or mock.Mock(returncode=0)):
            self.assertTrue(pulse.set_source_volume("src", 150))
            self.assertTrue(pulse.set_source_volume("src", -5))
        self.assertEqual(calls[0][-1], "100%")
        self.assertEqual(calls[1][-1], "0%")


class TrimVolumeTest(unittest.TestCase):
    def test_within_tolerance_is_left_alone(self):
        plan = pulse.trim_volume(-14.5, 80)
        self.assertEqual(plan["action"], "none")
        self.assertEqual(plan["reason"], "within tolerance")

    def test_too_loud_lowers_by_at_most_six_db(self):
        plan = pulse.trim_volume(-2.0, 100, target_db=-14.0, max_step_db=6.0)
        self.assertEqual(plan["action"], "lower")
        self.assertAlmostEqual(plan["percent"], 50.1, delta=0.2)
        self.assertAlmostEqual(plan["delta_db"], -12.0)

    def test_too_quiet_raises(self):
        plan = pulse.trim_volume(-30.0, 50, target_db=-14.0, max_step_db=6.0)
        self.assertEqual(plan["action"], "raise")
        self.assertGreater(plan["percent"], 50)

    def test_digital_silence_is_never_amplified(self):
        plan = pulse.trim_volume(-120.0, 50)
        self.assertEqual(plan["action"], "none")
        self.assertIn("silence", plan["reason"])

    def test_already_at_100_cannot_raise(self):
        plan = pulse.trim_volume(-30.0, 100, target_db=-14.0, max_step_db=6.0)
        self.assertEqual(plan["action"], "none")
        self.assertIn("100%", plan["reason"])

    def test_unknown_values(self):
        self.assertEqual(pulse.trim_volume(None, 50)["action"], "none")
        self.assertEqual(pulse.trim_volume(-14.0, None)["action"], "none")


if __name__ == "__main__":
    unittest.main()
