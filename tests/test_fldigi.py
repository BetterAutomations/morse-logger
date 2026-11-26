"""Tests for the fldigi engine's configuration and carrier search.

Nothing here starts fldigi: the config file it would read, and the quality
search that nudges the carrier, are both testable with a stub.
"""

import os
import socket
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from morse_logger import fldigi  # noqa: E402


class MatchedFilterTest(unittest.TestCase):
    def test_one_dot_long(self):
        self.assertEqual(fldigi.matched_filter_ms(20), 60)
        self.assertEqual(fldigi.matched_filter_ms(45), 27)

    def test_clamped(self):
        self.assertEqual(fldigi.matched_filter_ms(100), 20)    # 12 ms -> 20 ms floor
        self.assertEqual(fldigi.matched_filter_ms(5), 150)     # 240 ms -> 150 ms cap

    def test_bad_input_falls_back_to_20_wpm(self):
        self.assertEqual(fldigi.matched_filter_ms("bad"), 60)
        self.assertEqual(fldigi.matched_filter_ms(0), 60)
        self.assertEqual(fldigi.matched_filter_ms(None), 60)


class ConfigFileTest(unittest.TestCase):
    def make(self, home, **kw):
        args = dict(home=home, input_device="morse-logger_cable_src", tone=1500,
                    wpm=20, bandwidth=150, matched_filter=True, port=7362,
                    binary="/bin/true")
        args.update(kw)
        return fldigi.Fldigi(**args)

    def test_writes_the_decoder_settings(self):
        with tempfile.TemporaryDirectory() as home:
            eng = self.make(home)
            eng.write_config()
            with open(os.path.join(home, "fldigi", "fldigi_def.xml")) as f:
                body = f.read()
        self.assertIn("<PORTINDEVICE>morse-logger_cable_src</PORTINDEVICE>", body)
        self.assertIn("<CWSWEETSPOT>1500</CWSWEETSPOT>", body)
        self.assertIn("<CWBANDWIDTH>150</CWBANDWIDTH>", body)
        self.assertIn("<CWDEFSPEED>20</CWDEFSPEED>", body)
        self.assertIn("<CWMFILTLEN>60</CWMFILTLEN>", body)
        self.assertIn("<CWUSEFARNSWORTH>0</CWUSEFARNSWORTH>", body)

    def test_farnsworth_is_recorded(self):
        with tempfile.TemporaryDirectory() as home:
            eng = self.make(home, farnsworth_wpm=5)
            eng.write_config()
            with open(os.path.join(home, "fldigi", "fldigi_def.xml")) as f:
                body = f.read()
        self.assertIn("<CWUSEFARNSWORTH>1</CWUSEFARNSWORTH>", body)
        self.assertIn("<CWFARNSWORTH>5</CWFARNSWORTH>", body)

    def test_textout_path_and_file(self):
        with tempfile.TemporaryDirectory() as home:
            eng = self.make(home)
            self.assertTrue(eng.textout.endswith(os.path.join("talk", "textout.txt")))
            eng.write_config()
            self.assertTrue(os.path.exists(eng.textout))


class RpcStub:
    def __init__(self, quality_fn, fail_at=None):
        self.hz = 1500
        self.q = quality_fn
        self.fail_at = fail_at
        self.sets = []

    def set_carrier(self, hz):
        self.sets.append(hz)
        if self.fail_at is not None and len(self.sets) > self.fail_at:
            raise RuntimeError("rpc gone")
        self.hz = hz

    def quality(self):
        return self.q(self.hz)


class RefineCarrierTest(unittest.TestCase):
    def test_a_marginal_difference_keeps_the_spectral_estimate(self):
        # quality saturates inside the filter: 10 Hz off is not a clear win
        def q(hz):
            return max(0.0, 1.0 - abs(hz - 1500) / 100.0) * 0.9
        rpc = RpcStub(q)
        hz, quality, tried = fldigi.refine_carrier(
            rpc, 1500, span=20, step=10, dwell=0.001, log=None)
        self.assertEqual(hz, 1500)
        self.assertAlmostEqual(quality, 0.9, delta=0.05)
        self.assertTrue(tried)

    def test_a_clear_improvement_is_taken(self):
        def q(hz):
            return 0.95 if hz == 1520 else 0.1
        rpc = RpcStub(q)
        hz, quality, _tried = fldigi.refine_carrier(
            rpc, 1500, span=20, step=10, dwell=0.001, log=None)
        self.assertEqual(hz, 1520)
        self.assertAlmostEqual(quality, 0.95, delta=0.05)
        self.assertEqual(rpc.hz, 1520)              # left where it ended up

    def test_rpc_failure_mid_sweep_is_survived(self):
        rpc = RpcStub(lambda hz: 0.5, fail_at=2)
        hz, _quality, tried = fldigi.refine_carrier(
            rpc, 1500, span=20, step=10, dwell=0.001, log=None)
        self.assertIsInstance(hz, int)
        self.assertTrue(tried)


class PortTest(unittest.TestCase):
    def test_a_listening_port_is_reported_busy(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            port = s.getsockname()[1]
            self.assertFalse(fldigi._port_free(port))

    def test_a_closed_port_is_free(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.assertTrue(fldigi._port_free(port))


class FindBinaryTest(unittest.TestCase):
    def test_explicit_path_wins(self):
        self.assertEqual(fldigi.find_binary("/opt/fldigi"), "/opt/fldigi")

    def test_missing_everywhere(self):
        import unittest.mock as mock
        with mock.patch("shutil.which", return_value=None), \
                mock.patch("os.path.exists", return_value=False):
            self.assertIsNone(fldigi.find_binary())


if __name__ == "__main__":
    unittest.main()
