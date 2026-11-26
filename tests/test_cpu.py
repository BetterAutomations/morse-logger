"""The CPU profile: measured once, cached, and never compounded into config."""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from morse_logger import cpu, logger  # noqa: E402


class ClassifyTest(unittest.TestCase):
    def test_thresholds(self):
        self.assertEqual(cpu.classify(0.028), "fast")
        self.assertEqual(cpu.classify(0.055), "fast")
        self.assertEqual(cpu.classify(0.056), "medium")
        self.assertEqual(cpu.classify(0.100), "medium")
        self.assertEqual(cpu.classify(0.101), "slow")
        self.assertEqual(cpu.classify(0.150), "slow")     # SU9300 territory

    def test_benchmark_is_positive(self):
        self.assertGreater(cpu.benchmark(reps=1), 0.0)


class DetectTest(unittest.TestCase):
    def test_cache_is_used(self):
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, "run"))
            with open(os.path.join(home, "run", cpu.CACHE), "w") as f:
                json.dump({"version": cpu.BENCH_VERSION, "seconds": 0.12,
                           "profile": "slow"}, f)
            with mock.patch.object(cpu, "benchmark") as bench:
                seconds, profile = cpu.detect(home)
            bench.assert_not_called()
            self.assertEqual((seconds, profile), (0.12, "slow"))

    def test_old_cache_version_is_remeasured(self):
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, "run"))
            with open(os.path.join(home, "run", cpu.CACHE), "w") as f:
                json.dump({"version": 0, "seconds": 9.9, "profile": "slow"}, f)
            seconds, profile = cpu.detect(home)
            self.assertNotEqual(seconds, 9.9)
            self.assertIn(profile, ("fast", "medium", "slow"))

    def test_force_ignores_the_cache(self):
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, "run"))
            with open(os.path.join(home, "run", cpu.CACHE), "w") as f:
                json.dump({"version": cpu.BENCH_VERSION, "seconds": 0.028,
                           "profile": "fast"}, f)
            with mock.patch.object(cpu, "benchmark", return_value=0.2):
                seconds, profile = cpu.detect(home, force=True)
            self.assertEqual((seconds, profile), (0.2, "slow"))

    def test_missing_directory_is_not_created(self):
        with tempfile.TemporaryDirectory() as home:
            seconds, profile = cpu.detect(home)
            self.assertGreater(seconds, 0)
            self.assertFalse(os.path.exists(os.path.join(home, "run")))

    def test_corrupt_cache_is_remeasured(self):
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, "run"))
            with open(os.path.join(home, "run", cpu.CACHE), "w") as f:
                f.write("{not json")
            seconds, _profile = cpu.detect(home)
            self.assertGreater(seconds, 0)


class ScaledConfigTest(unittest.TestCase):
    """The scale multiplies at read time, so saving never compounds it."""

    def test_slow_profile_stretches_the_live_value(self):
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.object(cpu, "detect", return_value=(0.2, "slow")):
                cfg = logger.load_config(home)
            self.assertEqual(cfg["_cpu_profile"], "slow")
            self.assertEqual(cfg["watchdog_silence_s"], 2400.0)   # 600 * 4

    def test_fast_profile_leaves_the_value_alone(self):
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.object(cpu, "detect", return_value=(0.028, "fast")):
                cfg = logger.load_config(home)
            self.assertEqual(cfg["_cpu_profile"], "fast")
            self.assertEqual(cfg["watchdog_silence_s"], 600)

    def test_saving_drops_runtime_keys(self):
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.object(cpu, "detect", return_value=(0.2, "slow")):
                cfg = logger.load_config(home)
            logger.save_config(cfg, home)
            with open(os.path.join(home, "config.json")) as f:
                on_disk = json.load(f)
            for key in ("_cpu_score", "_cpu_profile", "_cpu_scale", "_home"):
                self.assertNotIn(key, on_disk)
            self.assertIn("watchdog_silence_s", on_disk)


if __name__ == "__main__":
    unittest.main()
