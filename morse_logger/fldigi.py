"""Drive fldigi headless as a pure CW decoding engine.

Why fldigi: it is the most capable open-source CW decoder available offline
(multi-stage filters, automatic speed tracking, matched filter, AFC/waterfall),
it is packaged for every Ubuntu release, and it will happily run without a
desktop on Xvfb.  We use only two of its interfaces:

  * the "rx text stream" (Configure > Misc > Rx text capture) which appends
    decoded characters to <config-dir>/talk/textout.txt as they are decoded,
  * its XML-RPC server, which we use to select the CW modem, set the decoder
    tone (carrier), and read signal quality.

fldigi shows a configuration wizard when its config file is missing, so we
provision a minimal fldigi_def.xml ourselves - that also pins the audio device
by *name* (PORTINDEVICE), which survives device renumbering.
"""
import os
import re
import shutil
import signal
import socket
import subprocess
import time
import xmlrpc.client

from . import pulse


def matched_filter_ms(wpm):
    """Matched-filter length in milliseconds for a given speed.

    A dot lasts 1200/wpm ms. A filter much longer than that averages across
    element boundaries and smears the very transitions the decoder slices on -
    at 45 WPM a 100 ms filter spans three and a half dots.
    """
    try:
        wpm = float(wpm)
    except (TypeError, ValueError):
        wpm = 20.0
    if wpm <= 0:
        wpm = 20.0
    return int(max(20, min(150, round(1200.0 / wpm))))

CONFIG_TEMPLATE = """<FLDIGI_DEFS>
<AUDIOIO>1</AUDIOIO>
<PORTINDEVICE>{device}</PORTINDEVICE>
<PORTOUTDEVICE>default</PORTOUTDEVICE>
<SPEAK>1</SPEAK>
<CONFIRMEXIT>0</CONFIRMEXIT>
<DUPLEX>1</DUPLEX>
<CWSWEETSPOT>{tone}</CWSWEETSPOT>
<CWTRACK>1</CWTRACK>
<CWBANDWIDTH>{bw}</CWBANDWIDTH>
<CWMFILT>{mfilt}</CWMFILT>
<CWMFILTLEN>{mfiltlen}</CWMFILTLEN>
<CWDEFSPEED>{wpm}</CWDEFSPEED>
<CWSPEED>{wpm}</CWSPEED>
<CWUSEFARNSWORTH>{farnsworth_use}</CWUSEFARNSWORTH>
<CWFARNSWORTH>{farnsworth_wpm}</CWFARNSWORTH>
</FLDIGI_DEFS>
"""


def find_binary(explicit=None):
    """Locate fldigi: explicit path, PATH, or the bundled runtime tree."""
    if explicit:
        return explicit
    p = shutil.which("fldigi")
    if p:
        return p
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for cand in (os.path.join(here, "runtime", "usr", "bin", "fldigi"),
                 "/opt/morse-logger/runtime/usr/bin/fldigi"):
        if os.path.exists(cand):
            return cand
    return None


def runtime_env(binary):
    """If fldigi comes from an extracted .deb tree, add its lib dirs."""
    env = dict(os.environ)
    root = os.path.dirname(os.path.dirname(os.path.dirname(binary)))  # .../runtime
    libdirs = [os.path.join(root, "usr", "lib", "x86_64-linux-gnu"),
               os.path.join(root, "lib", "x86_64-linux-gnu"),
               os.path.join(root, "usr", "lib")]
    libdirs = [d for d in libdirs if os.path.isdir(d)]
    if libdirs:
        old = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = ":".join(libdirs + ([old] if old else []))
    return env


class Rpc:
    """Thin XML-RPC client with a short timeout."""

    def __init__(self, port, host="127.0.0.1", timeout=3.0):
        self.port = port
        self.host = host
        self.timeout = timeout
        self._proxy = None

    def _p(self):
        if self._proxy is None:
            transport = xmlrpc.client.Transport()
            transport.timeout = self.timeout
            url = f"http://{self.host}:{self.port}"
            self._proxy = xmlrpc.client.ServerProxy(url, transport=transport, allow_none=True)
        return self._proxy

    def call(self, method, *args):
        try:
            return getattr(self._p(), method)(*args)
        except Exception:
            self._proxy = None
            raise

    # convenience wrappers
    def modem_name(self):
        return self.call("modem.get_name")

    def set_modem(self, name="CW"):
        return self.call("modem.set_by_name", name)

    def carrier(self):
        return int(self.call("modem.get_carrier"))

    def set_carrier(self, hz):
        return int(self.call("modem.set_carrier", int(round(hz))))

    def bandwidth(self):
        return int(self.call("modem.get_bandwidth"))

    def quality(self):
        try:
            return round(float(self.call("modem.get_quality")), 3)
        except Exception:
            return None

    def status1(self):
        try:
            return str(self.call("main.get_status1"))
        except Exception:
            return ""

    def rf_frequency(self):
        try:
            return float(self.call("rig.get_frequency"))
        except Exception:
            try:
                return float(self.call("main.get_frequency"))
            except Exception:
                return None

    def trx_status(self):
        try:
            return str(self.call("main.get_trx_status"))
        except Exception:
            return ""


def refine_carrier(rpc, center, span=20, step=10, dwell=0.9, log=print):
    """Nudge the decoder carrier to where the signal actually decodes best.

    A spectrum peak tells you roughly where the tone is; only the decoder knows
    whether the copy is clean. Sweep +/- span Hz, sample modem.get_quality()
    (max, because it reads 0 between characters), and keep the best.
    """
    best_hz, best_q = int(center), -1.0
    tried = []
    for off in (0, -step, step, -2 * step, 2 * step, -3 * step, 3 * step,
                -4 * step, 4 * step):
        hz = int(round(center + off))
        if abs(off) > span:
            continue
        try:
            rpc.set_carrier(hz)
        except Exception:
            break
        q = 0.0
        deadline = time.time() + dwell
        while time.time() < deadline:
            v = rpc.quality()
            if v is not None and v > q:
                q = float(v)
            time.sleep(0.1)
        tried.append((hz, round(q, 1)))
        if q > best_q:
            best_hz, best_q = hz, q
    # Quality saturates: within a 150 Hz filter a strong signal reads well across
    # tens of Hz, so the maximum is noise and can sit at the filter edge, far from
    # the real note. Move only for a *clear* improvement, and then only among
    # candidates that are both nearly as good and close to the spectral estimate.
    if tried and best_q > 0:
        base = dict(tried).get(int(round(center)), 0.0)
        if best_q < base * 1.15:
            best_hz, best_q = int(round(center)), base
        else:
            close = [(abs(hz - center), hz, q) for hz, q in tried
                     if q >= best_q * 0.95 and q > 0]
            if close:
                _d, best_hz, best_q = min(close)
    try:
        rpc.set_carrier(best_hz)
    except Exception:
        pass
    if log:
        log("refine: " + "  ".join(f"{hz}:{q}" for hz, q in tried))
        log(f"refine: keeping {best_hz} Hz (quality {best_q})")
    return best_hz, best_q, tried


def _free_display(start=99):
    for n in range(start, start + 40):
        if not os.path.exists(f"/tmp/.X11-unix/X{n}"):
            return n
    raise RuntimeError("no free X display found")


def _port_free(port):
    """True when nothing is *accepting* connections there.

    Binding to test is the wrong question: after a burst of restarts the port is
    full of TIME_WAIT sockets, and bind() fails even though no process listens -
    which is how a dead decoder kept reporting "port already in use" and looping
    forever. Asking whether something answers is the question we actually mean.
    """
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) != 0


def stray_pids(config_dir):
    """pids of fldigi processes using *our* config dir - leftovers from a dead
    daemon or a failed restart. They hold the XML-RPC port and the audio device,
    so a new engine cannot start while they live."""
    found = []
    cfg = os.path.abspath(config_dir)
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            raw = open(f"/proc/{name}/cmdline", "rb").read()
        except OSError:
            continue
        args = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
        if not args or os.path.basename(args[0]) != "fldigi":
            continue
        if any(os.path.abspath(a) == cfg for a in args[1:]):
            found.append(int(name))
    return found


def reap_strays(config_dir, log=None):
    killed = []
    for pid in stray_pids(config_dir):
        try:
            os.kill(pid, signal.SIGKILL)
            killed.append(pid)
        except OSError:
            pass
    if killed and log:
        log(f"killed {len(killed)} stray fldigi process(es) left by a previous run: "
            f"{', '.join(str(k) for k in killed)}")
    return killed


class Fldigi:
    """Headless fldigi instance (Xvfb + XML-RPC + rx text stream)."""

    def __init__(self, home, input_device=pulse.SOURCE, tone=1500, wpm=20,
                 bandwidth=150, matched_filter=True, port=7362, binary=None,
                 display=None, log=print, farnsworth_wpm=0):
        self.home = home                      # morse-logger home dir
        self.config_dir = os.path.join(home, "fldigi")
        self.input_device = input_device
        self.tone = tone
        self.wpm = wpm
        self.farnsworth_wpm = int(farnsworth_wpm or 0)
        self.bandwidth = bandwidth
        self.matched_filter = 1 if matched_filter else 0
        self.port = port
        self.binary = find_binary(binary)
        self.display = display
        self.log = log
        self.proc = None
        self.xvfb = None
        self.rpc = Rpc(port)
        self.pidfile = os.path.join(home, "run", "fldigi.pid")

    # ---------- config ----------
    @property
    def textout(self):
        return os.path.join(self.config_dir, "talk", "textout.txt")

    def write_config(self):
        os.makedirs(os.path.join(self.config_dir, "talk"), exist_ok=True)
        path = os.path.join(self.config_dir, "fldigi_def.xml")
        body = CONFIG_TEMPLATE.format(device=self.input_device, tone=int(self.tone),
                                      bw=int(self.bandwidth), mfilt=self.matched_filter,
                                      wpm=int(round(self.wpm)),
                                      mfiltlen=matched_filter_ms(self.wpm),
                                      farnsworth_use=1 if self.farnsworth_wpm else 0,
                                      farnsworth_wpm=self.farnsworth_wpm or 0)
        with open(path, "w") as f:
            f.write(body)
        # fldigi truncates textout.txt when it starts; make sure the dir exists
        if not os.path.exists(self.textout):
            open(self.textout, "w").close()

    # ---------- process ----------
    def display_alive(self):
        """Is the X display we are using still there?

        Xvfb can be killed (or crash) while fldigi is running; fldigi then dies
        with it and every restart fails, so the daemon has to notice and start a
        fresh display rather than counting the failures.
        """
        if not self.display:
            return True
        m = re.match(r":(\d+)", self.display)
        if m and os.path.exists(f"/tmp/.X11-unix/X{m.group(1)}"):
            return self.xvfb is None or self.xvfb.poll() is None
        return False

    def _start_xvfb(self):
        if self.display:
            if self.display_alive():
                return
            # the old server is gone: drop the stale handle and start a new one
            self.xvfb = None
            self.display = None
        if shutil.which("Xvfb"):
            n = _free_display()
            self.display = f":{n}"
            self.xvfb = subprocess.Popen(
                ["Xvfb", self.display, "-screen", "0", "1024x768x24", "-nolisten", "tcp"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(50):
                if os.path.exists(f"/tmp/.X11-unix/X{n}"):
                    break
                time.sleep(0.1)
        else:
            self.display = os.environ.get("DISPLAY") or ":0"

    def is_running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, timeout=25):
        if not self.binary:
            raise RuntimeError("fldigi binary not found (looked on PATH, ./runtime and /opt/morse-logger/runtime)")
        if not _port_free(self.port):
            # our own leftovers are the usual cause; a genuinely foreign listener
            # still fails, with the same message as before
            if reap_strays(self.config_dir, log=getattr(self, "log", None)):
                time.sleep(0.7)
        if not _port_free(self.port):
            raise RuntimeError(f"xmlrpc port {self.port} is already in use "
                               f"(another morse_logger/fldigi running?)")
        os.makedirs(os.path.join(self.home, "run"), exist_ok=True)
        self.write_config()
        self._start_xvfb()
        env = runtime_env(self.binary)
        env["DISPLAY"] = self.display
        env.pop("PULSE_SOURCE", None)
        env.pop("PIPEWIRE_NODE", None)
        args = [self.binary,
                "--config-dir", self.config_dir,
                "--home-dir", self.home,
                "--xmlrpc-server-port", str(self.port)]
        self.proc = subprocess.Popen(args, env=env, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True)
        # wait for RPC
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"fldigi exited immediately (code {self.proc.returncode})")
            try:
                self.rpc.call("fldigi.name")
                break
            except Exception as e:
                last = e
                time.sleep(0.4)
        else:
            raise RuntimeError(f"fldigi XML-RPC did not come up on :{self.port} ({last})")
        self.rpc.set_modem("CW")
        self.rpc.set_carrier(self.tone)
        try:
            with open(self.pidfile, "w") as f:
                f.write(str(self.proc.pid))
        except OSError:
            pass
        self.log(f"fldigi ready (pid {self.proc.pid}, display {self.display}, "
                 f"input {self.input_device}, CW tone {self.tone:g} Hz)")
        return self

    def stop(self, timeout=8):
        if self.xvfb is not None:
            try:
                self.xvfb.terminate()
                self.xvfb.wait(timeout=3)
            except Exception:
                self.xvfb.kill()
            self.xvfb = None
        if self.proc is None:
            return
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        except Exception:
            try:
                self.proc.terminate()
            except Exception:
                pass
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            except Exception:
                self.proc.kill()
        self.proc = None
        try:
            os.unlink(self.pidfile)
        except OSError:
            pass

    def healthy(self):
        if not self.is_running():
            return False
        try:
            self.rpc.call("fldigi.name")
            return True
        except Exception:
            return False


def stop_stale(home, log=print):
    """Kill a fldigi left behind by a crashed morse-logger run."""
    pidfile = os.path.join(home, "run", "fldigi.pid")
    try:
        pid = int(open(pidfile).read().strip())
    except Exception:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
        log(f"stopped stale fldigi pid {pid}")
        return True
    except OSError:
        pass
    try:
        os.unlink(pidfile)
    except OSError:
        pass
    return False
