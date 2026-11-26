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
\tSink: 71
\tSample Specification: float32le 1ch 48000Hz
\tapplication.name = "pw-play"
\tmedia.name = "/tmp/tone.wav"
\tCorked: no

Sink Input #172927
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

    def test_blank_output(self):
        with mock.patch.object(pulse, "_run", return_value=mock.Mock(stdout=b"")), \
                mock.patch.object(pulse, "sinks", return_value=[]):
            self.assertEqual(pulse.sink_input_details(), [])


class ModuleTest(unittest.TestCase):
    def test_monitor_and_tap_are_distinguished(self):
        with mock.patch.object(pulse, "_module_blocks", return_value=[
                ("51", MODULES.split("Module #")[1]),
                ("52", MODULES.split("Module #")[2]),
                ("53", MODULES.split("Module #")[3])]):
            self.assertTrue(pulse._monitor_active())
            self.assertTrue(pulse._tap_active())
            self.assertTrue(pulse._is_tap_block(MODULES.split("Module #")[2]))
            self.assertFalse(pulse._is_tap_block(MODULES.split("Module #")[1]))

    # FIXME.txt, "stop_source_loops() skips ...": a line-in loop into the cable
    # (source=alsa_input..., sink=cable) is not a tap, but _is_tap_block() only
    # checks the sink. Documented here so fixing it turns this into a pass.
    @unittest.expectedFailure
    def test_line_in_loop_is_not_a_tap(self):
        self.assertFalse(pulse._is_tap_block(MODULES.split("Module #")[3]))

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


class SourceNamesTest(unittest.TestCase):
    def test_monitors_and_our_cables_are_filtered_out(self):
        rows = ("1\talsa_input.usb-mic\tmodule-alsa-card.c\n"
                "2\talsa_output.analog-stereo.monitor\tmodule-alsa-card.c\n"
                "3\tmorse-logger_cable_src\tmodule-remap-source.c\n"
                "4\tmorse-logger_cable.monitor\tmodule-null-sink.c\n")
        with mock.patch.object(pulse, "_run", return_value=mock.Mock(stdout=rows.encode())):
            self.assertEqual(pulse.source_names(), ["alsa_input.usb-mic"])


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


if __name__ == "__main__":
    unittest.main()
