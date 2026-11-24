"""Supervisor process: rtl_fm -> virtual cable. Started by `morse-logger sdr start`."""
import argparse
import os
import signal
import sys
from datetime import datetime

from . import logger, pulse, sdr

_STOP = {"now": False}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="morse-logger sdr_run")
    ap.add_argument("--freq", type=float, required=True, help="Hz")
    ap.add_argument("--mode", default="usb", choices=list(sdr.CW_MODES))
    ap.add_argument("--device", default=None)
    ap.add_argument("--ppm", type=int, default=0)
    ap.add_argument("--gain", default=None)
    ap.add_argument("--sample-rate", type=int, default=24000)
    ap.add_argument("--squelch", type=int, default=0)
    ap.add_argument("--hf", action="store_true")
    ap.add_argument("--bias-tee", action="store_true")
    ap.add_argument("--extra", nargs="*", default=None)
    args = ap.parse_args(argv)

    cfg = logger.load_config()
    log_dir = os.path.join(cfg["_home"], "run")
    os.makedirs(log_dir, exist_ok=True)

    def say(msg):
        print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)

    pulse.ensure_cable(state_file=os.path.join(log_dir, "pulse-modules.json"),
                       monitor=bool(cfg.get("monitor")), tap=bool(cfg.get("tap")))
    cmd = sdr.build_cmd(args.freq, args.mode, args.device, args.ppm, args.gain,
                        hf=args.hf, bias_tee=args.bias_tee,
                        sample_rate=args.sample_rate, squelch=args.squelch,
                        extra=args.extra)
    say(f"sdr: {' '.join(cmd)}")
    say(f"sdr: routing into {pulse.SINK} at {args.sample_rate} Hz")
    pipe = sdr.Pipe(cmd, pulse.SINK, sample_rate=args.sample_rate, log=say)

    def _stop(_sig, _frm):
        _STOP["now"] = True
        pipe.stop()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        pipe.run()
    finally:
        say("sdr: stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
