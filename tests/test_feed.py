"""What is feeding the decoder: the classifier and the wording.

None of these call pactl; they take the gathered rows, so the rules can be
tested without a sound server. The gathering itself is feed.describe().
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from morse_logger import feed  # noqa: E402

CABLE = "morse-logger_cable"


def cfg(**kw):
    base = {"_home": tempfile.gettempdir(),
            "input_device": "morse-logger_cable_src"}
    base.update(kw)
    return base


def stream(app, media="Playback Stream", index=1, sink=CABLE, module="99"):
    return {"index": index, "app": app, "stream": media,
            "sink_name": sink, "module": module}


class KindTest(unittest.TestCase):
    def test_browsers(self):
        for app in ("Google Chrome", "Firefox", "Chromium", "Brave"):
            self.assertEqual(feed.kind(app), "browser", app)

    def test_players(self):
        for app in ("VLC media player", "mpv", "Rhythmbox", "Spotify"):
            self.assertEqual(feed.kind(app), "player", app)

    def test_unknown_app_gets_no_claim(self):
        self.assertEqual(feed.kind("some-dsp-tool"), "")

    def test_a_browser_tab_named_in_the_stream_still_counts(self):
        # Chrome often reports application.name as the binary; the media name
        # is the tab title, which is the only place "WebSDR" appears.
        self.assertEqual(feed.kind("Chromium", "NA5B WebSDR"), "browser")


class SummaryTest(unittest.TestCase):
    def test_chosen_device_wins(self):
        text = feed.summary(cfg(input_device="alsa_input.usb-X"))
        self.assertIn("usb-X", text)
        self.assertIn("chosen capture device", text)

    def test_running_stick_reports_where_it_is_tuned(self):
        rtl = [{"pid": 42, "freq": 144178000, "mode": "usb", "gain": "40.2",
                "ppm": None}]
        self.assertEqual(feed.summary(cfg(), rtl=rtl, sdr=True),
                         "RTL-SDR: rtl_fm (pid 42) at 144.178 MHz USB")

    def test_configured_but_dead_stick_is_named_as_such(self):
        self.assertIn("not running", feed.summary(cfg(), sdr=True))

    def test_one_browser_stream(self):
        text = feed.summary(cfg(), streams=[stream("Google Chrome")])
        self.assertIn("Google Chrome (browser)", text)
        self.assertIn(f"-> {CABLE}", text)

    def test_several_streams_are_counted_not_listed(self):
        rows = [stream("Google Chrome"), stream("VLC media player", index=2)]
        text = feed.summary(cfg(), streams=rows)
        self.assertIn("2 programs", text)
        self.assertIn("Google Chrome", text)

    def test_hardware_loop_is_named(self):
        rows = [("12", "alsa_input.pci-0000_00_1f.3.HiFi__Line__source", CABLE)]
        text = feed.summary(cfg(), loop_rows=rows)
        self.assertIn("HiFi__Line__source", text)
        self.assertIn("looped into", text)

    def test_nothing_feeding_it_says_so(self):
        self.assertIn("nothing feeding it", feed.summary(cfg()))


class DetailsTest(unittest.TestCase):
    def test_device_line_names_what_the_decoder_opens(self):
        lines = feed.details(cfg())
        self.assertEqual(lines[0], "device   morse-logger_cable_src (the cable)")

    def test_a_real_device_is_not_called_the_cable(self):
        lines = feed.details(cfg(input_device="alsa_input.usb-X"))
        self.assertEqual(lines[0], "device   alsa_input.usb-X")

    def test_stream_line_carries_the_tab_title(self):
        lines = feed.details(cfg(), streams=[stream("Chromium", "NA5B WebSDR", index=7)])
        self.assertIn('stream   7  Chromium (browser)  "NA5B WebSDR"', lines[-1])

    def test_loop_line(self):
        lines = feed.details(cfg(), loop_rows=[("12", "alsa_input.usb-X", CABLE)])
        self.assertIn("loop     module 12  alsa_input.usb-X -> " + CABLE, lines)

    def test_missing_loop_names_the_reason(self):
        with mock.patch.object(feed.pulse, "source_exists", return_value=False):
            lines = feed.details(cfg(source_loops=["other_cable.monitor"]))
        self.assertIn("missing  other_cable.monitor (configured, "
                      "no such capture source)", lines)

    def test_loop_that_exists_but_is_not_loaded(self):
        with mock.patch.object(feed.pulse, "source_exists", return_value=True):
            lines = feed.details(cfg(source_loops=["alsa_input.usb-X"]))
        self.assertIn("missing  alsa_input.usb-X (configured, source exists but "
                      "nothing loops it into the cable)", lines)

    def test_a_loaded_loop_is_not_called_missing(self):
        lines = feed.details(cfg(source_loops=["alsa_input.usb-X"]),
                             loop_rows=[("12", "alsa_input.usb-X", CABLE)])
        self.assertFalse(any(line.startswith("missing") for line in lines))


class DescribeTest(unittest.TestCase):
    """The gathering step, with the sound server and /proc stubbed out."""

    def test_an_sdr_feed_is_described_by_its_own_argv_not_by_pacat(self):
        rows = [stream("pacat", "Playback Stream", index=3),
                stream("Google Chrome", "NA5B WebSDR", index=4)]
        rtl = [{"pid": 42, "freq": 7030000, "mode": "usb", "gain": "auto",
                "ppm": 0, "cmd": ["rtl_fm", "-f", "7030000"]}]
        with mock.patch.object(feed, "sdr_running", return_value=True), \
                mock.patch.object(feed, "rtl_processes", return_value=rtl), \
                mock.patch.object(feed.pulse, "sink_input_details", return_value=rows), \
                mock.patch.object(feed.pulse, "loopback_modules", return_value=[]), \
                mock.patch.object(feed.pulse, "our_modules", return_value=set()):
            summary, detail = feed.describe(cfg())
        self.assertIn("rtl_fm (pid 42) at 7.030 MHz", summary)
        text = "\n".join(detail)
        self.assertIn("rtl      pid 42", text)
        # pacat is rtl_fm's own pipe, not a second program feeding the cable
        self.assertNotIn("stream   3", text)
        # a browser tab playing at the same time is still worth naming
        self.assertIn("stream   4  Google Chrome (browser)", text)

    def test_a_cable_fed_by_a_browser(self):
        rows = [stream("Google Chrome", "NA5B WebSDR", index=5)]
        with mock.patch.object(feed, "sdr_running", return_value=False), \
                mock.patch.object(feed.pulse, "sink_input_details", return_value=rows), \
                mock.patch.object(feed.pulse, "loopback_modules", return_value=[]), \
                mock.patch.object(feed.pulse, "our_modules", return_value=set()):
            summary, detail = feed.describe(cfg())
        self.assertEqual(summary, f"Google Chrome (browser) -> {CABLE}")
        self.assertTrue(any(line.startswith(
            'stream   5  Google Chrome (browser)  "NA5B WebSDR"') for line in detail))

    def test_empty_cable_is_named(self):
        with mock.patch.object(feed, "sdr_running", return_value=False), \
                mock.patch.object(feed.pulse, "sink_input_details", return_value=[]), \
                mock.patch.object(feed.pulse, "loopback_modules", return_value=[]), \
                mock.patch.object(feed.pulse, "our_modules", return_value=set()), \
                mock.patch.object(feed.pulse, "sink_exists", return_value=True):
            summary, _detail = feed.describe(cfg())
        self.assertIn("nothing feeding it", summary)

    def test_missing_cable_is_not_reported_as_nothing_playing(self):
        with mock.patch.object(feed, "sdr_running", return_value=False), \
                mock.patch.object(feed.pulse, "sink_input_details", return_value=[]), \
                mock.patch.object(feed.pulse, "loopback_modules", return_value=[]), \
                mock.patch.object(feed.pulse, "our_modules", return_value=set()), \
                mock.patch.object(feed.pulse, "sink_exists", return_value=False):
            summary, _detail = feed.describe(cfg())
        self.assertIn("no such sink", summary)
        self.assertIn("cable up", summary)


if __name__ == "__main__":
    unittest.main()