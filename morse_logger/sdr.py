"""Feed a hardware RTL-SDR into the virtual cable.

The decoder does not care where the audio comes from, so hardware support is
just: run `rtl_fm` and pipe its 16-bit mono output into the cable sink. That is
all this module does - no SDR GUI, no dependency of the decoder on librtlsdr.

Two dongles need different settings:

* generic DVB-T stick (R820T/R820T2 tuner): 24 MHz - 1.7 GHz only. Use it for
  10 m and up. No direct sampling, so no HF.
* RTL-SDR Blog V3: same tuner range *plus* HF by direct sampling (Q branch,
  `-E direct2`) below ~24 MHz, and a bias tee (`-T`). Direct sampling bypasses
  the tuner, so tuner gain is meaningless there - pass 0.

One system caveat that catches everybody: the kernel's DVB driver
(dvb_usb_rtl28xxu) claims these sticks at plug-in, after which librtlsdr fails
with "usb_claim_interface error -6". The rtl-sdr package ships a blacklist for
it; it takes effect after a replug or reboot.
"""
import collections
import os
import shutil
import signal
import subprocess
import sys
import threading
import time

CW_MODES = ("usb", "lsb", "am")
HF_LIMIT_HZ = 24_000_000        # above this the tuner works normally


def binary(name="rtl_fm"):
    """Find the tool: PATH, then the bundled runtime directory."""
    found = shutil.which(name)
    if found:
        return found
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for cand in (os.path.join(here, "runtime", "usr", "bin", name),
                 os.path.join(here, "..", "runtime", "usr", "bin", name),
                 os.path.join("/opt", "morse-logger", "runtime", "usr", "bin", name)):
        if os.path.exists(cand):
            return os.path.abspath(cand)
    return None


def library_path():
    """librtlsdr for a bundled (unextracted-to-/usr) rtl_fm."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parts = [os.path.join(here, "runtime", "usr", "lib", "x86_64-linux-gnu"),
             "/opt/morse-logger/runtime/usr/lib/x86_64-linux-gnu"]
    parts = [p for p in parts if os.path.isdir(p)]
    return ":".join(parts) if parts else None


def build_cmd(freq_hz, mode="usb", device=None, ppm=0, gain=None, hf=False,
              bias_tee=False, sample_rate=24000, squelch=0, extra=None):
    """rtl_fm argv (writes raw s16 mono to stdout)."""
    exe = binary("rtl_fm")
    if not exe:
        raise RuntimeError("rtl_fm not found - install the rtl-sdr package "
                           "(or run install.sh from the bundle)")
    cmd = [exe, "-f", str(int(freq_hz)), "-M", mode, "-s", str(int(sample_rate)),
           "-l", str(int(squelch)), "-E", "dc"]
    if device not in (None, ""):
        cmd += ["-d", str(device)]
    cmd += ["-p", str(int(ppm))]
    if hf:
        # direct sampling Q branch: no tuner in the path, so gain is meaningless
        cmd += ["-E", "direct2", "-g", "0"]
    elif gain not in (None, "", "auto"):
        cmd += ["-g", str(gain)]
    if bias_tee:
        cmd += ["-T"]
    if extra:
        cmd += list(extra)
    cmd += ["-"]
    return cmd


def player_cmd(sink, sample_rate=24000):
    """argv that reads raw s16 mono on stdin and plays it into the cable sink."""
    if shutil.which("pacat"):
        return ["pacat", "--playback", f"--device={sink}", "--format=s16le",
                f"--rate={sample_rate}", "--channels=1", "--raw",
                "--latency-msec=100"]
    if shutil.which("pw-cat"):
        return ["pw-cat", "--playback", f"--target={sink}", "--format=s16",
                f"--rate={sample_rate}", "--channels=1", "-"]
    raise RuntimeError("neither pacat nor pw-cat found - cannot feed the cable")


def devices(timeout=6.0):
    """Enumerate sticks via rtl_test -t. Returns (list, raw_output)."""
    exe = binary("rtl_test") or binary("rtl_test")
    if not exe:
        return [], "rtl_test not found"
    env = dict(os.environ)
    lp = library_path()
    if lp:
        env["LD_LIBRARY_PATH"] = lp + (":" + env["LD_LIBRARY_PATH"]
                                       if env.get("LD_LIBRARY_PATH") else "")
    try:
        p = subprocess.run([exe, "-t"], capture_output=True, text=True,
                           timeout=timeout, env=env)
        raw = (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired as exc:
        raw = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        raw = raw or "timed out"
    found = []
    cur = None
    for line in raw.splitlines():
        s = line.strip()
        if s.lower().startswith("found") and "device" in s.lower():
            if cur:
                found.append(cur)
            cur = {"index": len(found), "desc": s}
        elif cur is not None and ("Serial number" in s or "serial" in s.lower()):
            cur["serial"] = s.split(":", 1)[-1].strip()
        elif cur is not None and ("Tuner type" in s or "tuner" in s.lower()):
            cur["tuner"] = s.split(":", 1)[-1].strip()
    if cur:
        found.append(cur)
    return found, raw


def describe(dev):
    """'#0 E4000 (serial 00000001)' - what to pass to -d."""
    bits = [f"#{dev['index']}"]
    if dev.get("tuner"):
        bits.append(dev["tuner"])
    if dev.get("serial"):
        bits.append(f"serial {dev['serial']}")
    return " ".join(bits)


class Pipe:
    """rtl_fm -> pacat, restarted when either side dies."""

    def __init__(self, cmd, sink, sample_rate=24000, log=print):
        self.cmd = cmd
        self.sink = sink
        self.sample_rate = sample_rate
        self.log = log
        self.rtl = None
        self.play = None
        self._stop = False

    def _spawn(self):
        env = dict(os.environ)
        lp = library_path()
        if lp:
            env["LD_LIBRARY_PATH"] = lp + (":" + env["LD_LIBRARY_PATH"]
                                           if env.get("LD_LIBRARY_PATH") else "")
        self.rtl = subprocess.Popen(self.cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, env=env)
        self.errlog = collections.deque(maxlen=40)
        # rtl_fm's stderr must be drained or the pipe buffer fills and the tuner
        # blocks forever. Keep the tail for diagnostics, surface real errors.
        def _drain(pipe, sink):
            for raw in iter(pipe.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                if not line:
                    continue
                sink.append(line)
                low = line.lower()
                if any(w in low for w in ("error", "fail", "no supported",
                                          "usb_claim", "tuned to")):
                    self.log(f"rtl_fm: {line}")

        threading.Thread(target=_drain, args=(self.rtl.stderr, self.errlog),
                         daemon=True).start()
        self.play = subprocess.Popen(player_cmd(self.sink, self.sample_rate),
                                     stdin=self.rtl.stdout,
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        self.rtl.stdout.close()          # let rtl_fm see EPIPE if pacat dies

    def run(self):
        backoff = 2
        while not self._stop:
            self._spawn()
            started = time.time()
            while not self._stop:
                if self.rtl.poll() is not None or self.play.poll() is not None:
                    break
                time.sleep(0.5)
            if self._stop:
                break
            err = "\n".join(list(self.errlog)[-5:])
            tail = " | ".join(ln.strip() for ln in err.splitlines()[-3:] if ln.strip())
            self.log(f"rtl_fm exited ({self.rtl.returncode}) after "
                     f"{int(time.time() - started)}s{': ' + tail if tail else ''}")
            if "usb_claim_interface" in err or "No supported devices" in err:
                self.log("hint: the kernel DVB driver is probably holding the "
                         "stick - blacklist dvb_usb_rtl28xxu and replug")
                self.stop()
                break
            for _ in range(backoff * 2):
                if self._stop:
                    break
                time.sleep(0.5)
            backoff = min(30, backoff * 2)
        self.kill()

    def kill(self):
        for p in (self.rtl, self.play):
            if p and p.poll() is None:
                try:
                    p.terminate()
                except Exception:                               # noqa: BLE001
                    pass
        time.sleep(0.3)
        for p in (self.rtl, self.play):
            if p and p.poll() is None:
                try:
                    p.kill()
                except Exception:                               # noqa: BLE001
                    pass

    def stop(self):
        self._stop = True
