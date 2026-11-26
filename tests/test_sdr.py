"""Tests for the RTL-SDR command line (no dongle attached)."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from morse_logger import sdr  # noqa: E402


class BuildCmdTest(unittest.TestCase):
    def setUp(self):
        self.patch = mock.patch.object(sdr, "binary", return_value="/usr/bin/rtl_fm")
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def test_basic_vhf_tuning(self):
        cmd = sdr.build_cmd(144_390_000)
        self.assertEqual(cmd[0], "/usr/bin/rtl_fm")
        self.assertEqual(cmd[cmd.index("-f") + 1], "144390000")
        self.assertEqual(cmd[cmd.index("-M") + 1], "usb")
        self.assertEqual(cmd[-1], "-")

    def test_hf_uses_direct_sampling_and_zero_gain(self):
        cmd = sdr.build_cmd(7_030_000, hf=True)
        self.assertIn("direct2", cmd)
        self.assertEqual(cmd[cmd.index("-g") + 1], "0")

    def test_tuner_gain_is_passed_above_hf(self):
        cmd = sdr.build_cmd(144_390_000, gain="40")
        self.assertEqual(cmd[cmd.index("-g") + 1], "40")

    def test_auto_gain_is_omitted(self):
        self.assertNotIn("-g", sdr.build_cmd(144_390_000, gain="auto"))

    def test_device_and_ppm(self):
        cmd = sdr.build_cmd(144_390_000, device="0", ppm=2)
        self.assertEqual(cmd[cmd.index("-d") + 1], "0")
        self.assertEqual(cmd[cmd.index("-p") + 1], "2")

    def test_bias_tee(self):
        self.assertIn("-T", sdr.build_cmd(144_390_000, bias_tee=True))

    def test_extra_arguments_go_before_the_output(self):
        cmd = sdr.build_cmd(144_390_000, extra=["-A", "fast"])
        self.assertEqual(cmd[-3:], ["-A", "fast", "-"])

    def test_missing_binary_is_a_clear_error(self):
        with mock.patch.object(sdr, "binary", return_value=None):
            with self.assertRaises(RuntimeError):
                sdr.build_cmd(144_390_000)


class PlayerCmdTest(unittest.TestCase):
    def test_pacat_is_preferred(self):
        with mock.patch("shutil.which", side_effect=lambda n: "/usr/bin/" + n):
            cmd = sdr.player_cmd("cable")
        self.assertEqual(cmd[0], "pacat")
        self.assertIn("--device=cable", cmd)
        self.assertIn("--raw", cmd)

    def test_pw_cat_is_the_fallback(self):
        with mock.patch("shutil.which", side_effect=lambda n: None if n == "pacat" else "/usr/bin/pw-cat"):
            cmd = sdr.player_cmd("cable")
        self.assertEqual(cmd[0], "pw-cat")
        self.assertIn("--target=cable", cmd)

    def test_neither_is_an_error(self):
        with mock.patch("shutil.which", return_value=None):
            with self.assertRaises(RuntimeError):
                sdr.player_cmd("cable")


class DevicesTest(unittest.TestCase):
    RTL_TEST = """Using device 0: Generic RTL2832U OEM
Found 1 device(s):
  0:  Realtek, RTL2838UHIDIR, SN: 00000001

Using device 0: Generic RTL2832U OEM
Detected Rafael Micro R820T tuner
Tuner gain set to automatic.
"""

    def test_parses_rtl_test_output(self):
        with mock.patch.object(sdr, "binary", return_value="/usr/bin/rtl_test"), \
                mock.patch("subprocess.run") as run:
            run.return_value.stdout = self.RTL_TEST
            run.return_value.stderr = ""
            found, raw = sdr.devices()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["index"], 0)
        self.assertIn("RTL2838", found[0]["desc"])
        self.assertIn("Found", raw)

    def test_missing_rtl_test(self):
        with mock.patch.object(sdr, "binary", return_value=None):
            found, raw = sdr.devices()
        self.assertEqual(found, [])
        self.assertIn("not found", raw)

    def test_describe(self):
        text = sdr.describe({"index": 1, "tuner": "E4000", "serial": "42"})
        self.assertIn("#1", text)
        self.assertIn("E4000", text)
        self.assertIn("serial 42", text)


class LimitsTest(unittest.TestCase):
    def test_hf_limit_is_24_mhz(self):
        self.assertEqual(sdr.HF_LIMIT_HZ, 24_000_000)

    def test_cw_modes(self):
        self.assertEqual(set(sdr.CW_MODES), {"usb", "lsb", "am"})


if __name__ == "__main__":
    unittest.main()
