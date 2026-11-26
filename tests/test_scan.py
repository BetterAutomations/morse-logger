"""Tests for band selection and peak scoring in the SDR survey (no dongle)."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from morse_logger import scan  # noqa: E402


class BandRangeTest(unittest.TestCase):
    def test_preset(self):
        lo, hi, label = scan.band_range("40m")
        self.assertEqual((lo, hi), (7_000_000, 7_125_000))
        self.assertIn("40m", label)

    def test_case_insensitive(self):
        self.assertEqual(scan.band_range("20M")[:2], (14_000_000, 14_070_000))

    def test_explicit_bounds(self):
        lo, hi, label = scan.band_range(start_khz=7030, stop_khz=7040)
        self.assertEqual((lo, hi), (7_030_000, 7_040_000))
        self.assertIn("7030-7040", label)

    def test_one_bound_is_refused(self):
        with self.assertRaises(ValueError):
            scan.band_range(start_khz=7030)

    def test_reversed_bounds_are_refused(self):
        with self.assertRaises(ValueError):
            scan.band_range(start_khz=7100, stop_khz=7000)

    def test_missing_band_is_refused(self):
        with self.assertRaises(ValueError):
            scan.band_range()

    def test_unknown_band_lists_the_choices(self):
        with self.assertRaises(ValueError) as ctx:
            scan.band_range("6m")
        self.assertIn("40m", str(ctx.exception))


class PowerCsvTest(unittest.TestCase):
    def parse(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f:
            f.write(text)
            path = f.name
        try:
            return scan._read_power_csv(path)
        finally:
            os.unlink(path)

    def test_middle_sample_of_each_sweep_is_kept(self):
        rows = self.parse(
            "2026-11-26, 12:00:00, 7000000, 7002000, 2000, 1, -90.5, -88.2\n"
            "2026-11-26, 12:00:00, 7002000, 7004000, 2000, 1, -87.0, -86.4\n")
        self.assertEqual(rows, {7001000: -88.2, 7003000: -86.4})

    def test_strongest_sweep_wins(self):
        rows = self.parse(
            "2026-11-26, 12:00:00, 7000000, 7002000, 2000, 1, -90.0\n"
            "2026-11-26, 12:00:01, 7000000, 7002000, 2000, 1, -80.0\n")
        self.assertEqual(rows, {7001000: -80.0})

    def test_short_and_non_numeric_lines_are_skipped(self):
        self.assertEqual(self.parse("garbage\n1,2,3\n2026-11-26, 12:00:00, x, y, z\n"),
                         {})

    def test_missing_file(self):
        self.assertEqual(scan._read_power_csv("/nonexistent/scan.csv"), {})


class PeaksTest(unittest.TestCase):
    def flat(self, n=40, value=-95.0, step=2000):
        return {7_000_000 + i * step: value for i in range(n)}

    def test_empty(self):
        self.assertEqual(scan._peaks({}), [])

    def test_narrow_carrier_is_found(self):
        bins = self.flat()
        bins[7_040_000] = -60.0
        bins[7_042_000] = -61.0
        out = scan._peaks(bins)
        self.assertEqual(len(out), 1)
        hz, db, prom = out[0]
        self.assertEqual(hz, 7_040_000)
        self.assertGreater(prom, 6.0)

    def test_flat_noise_has_no_peaks(self):
        self.assertEqual(scan._peaks(self.flat()), [])

    def test_a_broad_rise_is_not_a_carrier(self):
        bins = self.flat()
        for i in range(15):
            bins[7_030_000 + i * 2000] = -60.0 + i
        self.assertEqual(scan._peaks(bins, max_bins=5), [])

    def test_results_are_ordered_by_prominence(self):
        bins = self.flat()
        bins[7_020_000] = -70.0
        bins[7_060_000] = -55.0
        out = scan._peaks(bins)
        self.assertEqual([row[0] for row in out], [7_060_000, 7_020_000])

    def test_close_peaks_are_merged_by_the_400hz_rule(self):
        bins = self.flat()
        bins[7_040_000] = -60.0
        bins[7_040_200] = -61.0
        out = scan._peaks(bins)
        self.assertEqual(len(out), 1)

    def test_limit_is_respected(self):
        bins = self.flat(n=60)
        for i in range(10):
            bins[7_010_000 + i * 6_000] = -60.0
        self.assertLessEqual(len(scan._peaks(bins, limit=3)), 3)


class ReportTest(unittest.TestCase):
    def collect(self, results):
        out = []
        scan.report(results, out=out.append)
        return "\n".join(out)

    def test_keyed_result(self):
        text = self.collect([{"freq_hz": 7_010_000, "level_dbfs": -40,
                              "tone_hz": 750, "wpm": 20.1, "confidence": 0.9}])
        self.assertIn("7.0100", text)
        self.assertIn("750 Hz", text)
        self.assertIn("20.1 WPM", text)
        self.assertIn("morse-logger sdr start --freq 7.0100", text)

    def test_carrier_without_keying(self):
        text = self.collect([{"freq_hz": 7_010_000, "tone_hz": 750}])
        self.assertIn("carrier, no keying", text)

    def test_nothing_there(self):
        text = self.collect([{"freq_hz": 7_010_000}])
        self.assertIn("no CW", text)

    def test_empty(self):
        self.assertIn("nothing to report", self.collect([]))


if __name__ == "__main__":
    unittest.main()
