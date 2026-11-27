"""morse-logger command line interface."""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time

from . import (__version__, autotune, feed, fldigi, logger, pulse, retention,
               sdr, selftest, translate, wpm)

PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------- helpers
def pidfile(home):
    return os.path.join(home, "run", "morse-logger.pid")


def read_pid(home):
    try:
        return int(open(pidfile(home)).read().strip())
    except Exception:
        return None


def alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def status_json(home):
    try:
        with open(os.path.join(home, "run", "status.json")) as f:
            return json.load(f)
    except Exception:
        return None


def fmt_uptime(s):
    s = int(s)
    h, m = divmod(s // 60, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s % 60:02d}s"


# ---------------------------------------------------------------- commands
def cmd_doctor(args):
    cfg = logger.load_config(create=False)
    print(f"morse-logger {__version__}  home {cfg['_home']}")
    ok = True

    def chk(name, good, detail=""):
        nonlocal ok
        ok = ok and bool(good)
        print(f"  [{'ok' if good else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))

    # data layout: a log_dir outside the data directory silently splits the logs
    home = cfg["_home"]
    logd = os.path.abspath(cfg["log_dir"])
    if logd.startswith(os.path.abspath(home) + os.sep):
        print(f"  [ok] log directory {logd}")
    else:
        print(f"  [!!] log directory {logd} is outside the data directory {home} "
              f"- run 'morse-logger config --set log_dir={home}/logs'")
    stale = os.path.expanduser("~/.cw-logger")
    if os.path.exists(stale):
        print(f"  [!!] {stale} still exists - a leftover from before the rename; "
              f"nothing reads it, you can delete it")
    st = status_json(cfg["_home"]) or {}
    if st.get("restarts", 0) > 5:
        print(f"  [!!] the decoder has been restarted {st['restarts']} times this "
              f"session - something is preventing it from staying up; see "
              f"{cfg['_home']}/run/morse-logger.out")
    print("\nSymptoms/decoder")
    bin_ = fldigi.find_binary(cfg.get("fldigi_binary"))
    chk("fldigi binary", bin_, bin_ or "not found (run install.sh)")
    chk("Xvfb (headless X)", shutil.which("Xvfb"), shutil.which("Xvfb") or "missing")
    chk("pactl", shutil.which("pactl"), "pulseaudio-utils / pipewire-pulse")
    chk("parec", shutil.which("parec"), "pulseaudio-utils / pipewire-pulse")
    chk("audio player", shutil.which("pw-play") or shutil.which("paplay"), "needed for selftest")
    print("\nHardware SDR (optional)")
    rf = sdr.binary("rtl_fm")
    print(f"  [{'ok' if rf else '--'}] rtl_fm"
          + (f" - {rf}" if rf else " - not installed; WebSDR/virtual-cable use is unaffected"))
    if rf:
        found, _raw = sdr.devices()
        print(f"  [{'ok' if found else '--'}] RTL-SDR device(s): {len(found)}"
              + ("" if found else " - none attached (or the DVB driver holds it: "
                 "blacklist dvb_usb_rtl28xxu)"))
    print("\nAudio into the cable")
    try:
        # The echo trap: our cable monitor looped to the speakers more than
        # once (or at all, when monitoring is supposed to be off). One such
        # loopback is the normal way you hear the cable; two copies of the
        # same audio ~100 ms apart is what gets reported as an echo.
        strays = pulse.monitor_loops_to_default()
        if len(strays) > 1:
            print(f"  [!!] {len(strays)} monitor loopbacks play the cable into your "
                  f"speakers (modules {', '.join(strays)}) - that is the doubled "
                  f"audio/echo; remove them with: morse-logger cable up "
                  f"{'--monitor' if cfg.get('monitor') else '--no-monitor'}")
        elif strays and not cfg.get("monitor"):
            print(f"  [!!] monitor loopback {strays[0]} plays the cable into your "
                  f"speakers although monitoring is off - remove it with: "
                  f"morse-logger cable up --no-monitor")
        elif strays:
            print("  [ok] one monitor loopback into your speakers (monitoring on)")
        else:
            print("  [ok] no monitor loopback into your speakers")
        rows = [r for r in pulse.sink_input_details()
                if r.get("sink_name") == pulse.SINK]
        if not rows:
            print("  [--] nothing is playing into the cable yet")
        real = [r for r in rows
                if not (r.get("stream") or "").startswith("loopback-")]
        for r in rows:
            kind = "morse-logger monitor" if (r.get("stream") or "").startswith("loopback-") else "source"
            print(f"  [ok] {r.get('app')} -> {kind} (stream {r.get('index')})")
        if real and pulse.cable_status().get("source"):
            sample = pulse.capture_samples(1.0, rate=8000, source=pulse.SOURCE)
            if not any(sample):
                print("  [!!] those streams are open and unmuted but delivering digital "
                      "silence - the application's audio engine has lost the output "
                      "device. Restart the application (a tab reload is not enough): "
                      "this happens when the cable is renamed or recreated while it "
                      "is playing.")
            else:
                db = wpm.peak_dbfs(sample)
                verdict = pulse.level_verdict(db, sdr=feed.sdr_running(cfg["_home"]))
                if verdict == "no signal in the passband":
                    print(f"  [--] cable level {db} dBFS - no signal in the passband; "
                          f"an rtl_fm feed has a fixed gain, so this is an empty "
                          f"band, not a volume to raise")
                elif verdict == "too quiet":
                    print(f"  [!!] cable level {db} dBFS - far too quiet to decode; "
                          f"raise the application volume (aim for -20 to -10 dBFS)")
                elif verdict == "clipping":
                    print(f"  [!!] cable level {db} dBFS - clipping; lower the volume")
                else:
                    print(f"  [ok] cable level {db} dBFS")
        feeds = [r for r in rows if (r.get("app") or "") in ("pacat", "pw-cat", "rtl_fm")
                 and not (r.get("stream") or "").startswith("loopback-")]
        tracked = alive(read_pid(cfg["_home"]))
        sdr_pid = None
        try:
            sdr_pid = int(open(sdr_pidfile(cfg["_home"])).read().strip())
        except (OSError, ValueError):
            pass
        if feeds and not alive(sdr_pid):
            print(f"  [!!] {len(feeds)} audio feed(s) into the cable with no tracked SDR "
                  f"process - a stray feed (test leftover?) will be logged as if it were "
                  f"radio: run 'morse-logger sdr --stop'")
    except Exception as exc:                     # noqa: BLE001
        print(f"  [--] could not inspect: {exc}")
    print("\nAudio server")
    try:
        info = pulse.server_info()
        chk("pulse/pipewire server", True, f"{info.get('Server Name','?')} {info.get('Server Version','')}")
    except Exception as e:
        chk("pulse/pipewire server", False, str(e))
    st = pulse.cable_status() if pulse.available() else {"sink": False, "source": False}
    chk("virtual cable", st.get("sink") and st.get("source"),
        f"sink={st.get('sink')} source={st.get('source')} monitor={st.get('monitor')} "
        f"(create with: morse-logger cable up)")
    print("\nLogger")
    pid = read_pid(cfg["_home"])
    print(f"  [{'ok' if alive(pid) else '--'}] logger running - "
          + (f"pid {pid}" if alive(pid) else "not running (start it with: morse-logger start)"))
    stj = status_json(cfg["_home"]) or {}
    if stj:
        fl = stj.get("fldigi", {})
        chk("decoder responding", fl.get("running"), f"modem={fl.get('modem')} tone={fl.get('tone_hz')}Hz")
        print(f"       lines logged: {stj.get('lines')}  uptime {fmt_uptime(stj.get('uptime_s', 0))}")
    print(f"\n  log dir: {cfg['log_dir']}")
    try:
        du = shutil.disk_usage(cfg["log_dir"] if os.path.isdir(cfg["log_dir"]) else cfg["_home"])
        chk("free disk space", du.free > 200 * 1024 * 1024, f"{du.free // (1024**2)} MB free")
    except Exception:
        pass
    bundle = os.path.join(PKG_PARENT, "bundle")
    if os.path.isdir(bundle):
        debs_dir = os.path.join(bundle, "debs")
        count = 0
        sets = []
        for entry in sorted(os.listdir(debs_dir)) if os.path.isdir(debs_dir) else []:
            path = os.path.join(debs_dir, entry)
            if os.path.isdir(path):
                n = len([f for f in os.listdir(path) if f.endswith(".deb")])
                if n:
                    sets.append(f"{entry} {n}")
                    count += n
            elif entry.endswith(".deb"):
                count += 1
        chk("offline bundle present", count > 0,
            f"{count} packages ({', '.join(sets)})" if sets
            else f"{count} packages in {bundle}")
    print("\n" + ("All checks passed." if ok else "Some checks FAILED (see above)."))
    return 0 if ok else 1


def _want_monitor(args, cfg):
    """Tri-state: --monitor, --no-monitor, or whatever the config says.

    A bare `cable up` must not re-enable monitoring: it runs from helper
    scripts and restore steps too, where an unexpected loopback is exactly
    the doubled-audio (echo) bug. Only an explicit flag changes anything.
    """
    if getattr(args, "no_monitor", False):
        return False
    if getattr(args, "monitor", False):
        return True
    return bool(cfg.get("monitor", True))


def cmd_cable(args):
    cfg = logger.load_config()
    sf = os.path.join(cfg["_home"], "run", "pulse-modules.json")
    if args.action == "up":
        want = _want_monitor(args, cfg)
        if want and pulse._tap_active():
            print("the copy loop is on (your speakers -> the cable), so monitoring "
                  "would feed the cable back into your speakers.")
            print("pick one: 'morse-logger route-audio --stop-copy' to remove it, "
                  "or 'morse-logger cable up --no-monitor'.")
            return 2
        removed = [] if want else pulse.stop_monitor(state_file=sf)
        channels = max(1, int(getattr(args, "channels", 1) or 1))
        done = {}
        for ch in range(1, channels + 1):
            done = pulse.ensure_cable(state_file=sf, monitor=(want and ch == 1),
                                      channel=ch)
            print(f"virtual cable {ch}: sink '{pulse.sink_name(ch)}' -> "
                  f"source '{pulse.source_name(ch)}'")
        if done.get("monitor_skipped"):
            print("monitoring left off: the copy loop is running "
                  "('morse-logger route-audio --stop-copy' to remove it)")
        if removed:
            print(f"monitoring off ({len(removed)} loopback(s) removed)")
        if done["monitor"] or pulse.cable_status()["monitor"]:
            print("monitoring enabled: you still hear the cable output on your speakers")
        if done["sink"]:
            print("(created just now)")
        # Persist only what was asked. `cable up --no-monitor` used to change
        # only the live state, so a reboot put monitoring back while
        # `start --no-monitor` survived; and persisting on every bare `cable
        # up` overwrote a saved "off" with True.
        if bool(cfg.get("monitor", True)) != want:
            cfg["monitor"] = want
            logger.save_config(cfg)
            print(f"config: monitor={str(want).lower()}")
    elif args.action == "down":
        only = getattr(args, "channels", 1) if args.channels > 1 else None
        removed = pulse.cable_down(state_file=sf, channel=only)
        if only is None:                # the whole cable is gone, and its loops
            cfg.update({"monitor": False, "tap": False, "source_loops": []})
        logger.save_config(cfg)
        if only:
            print(f"cable {only} removed ({len(removed)} module(s) unloaded)")
        else:
            print(f"all cables removed ({len(removed)} module(s) unloaded)")
    else:
        st = pulse.cable_status()
        print(f"sink   {pulse.SINK}: {'present' if st['sink'] else 'MISSING'}")
        print(f"source {pulse.SOURCE}: {'present' if st['source'] else 'MISSING'}")
        print(f"monitor loopback: {'on' if st['monitor'] else 'off'}")
        print("\nsinks:    " + ", ".join(st["sinks"]))
        print("sources:  " + ", ".join(st["sources"]))
    return 0


def cmd_route(args):
    cfg = logger.load_config()
    sf = os.path.join(cfg["_home"], "run", "pulse-modules.json")
    wants_move = bool(args.all or args.index is not None)
    wants_copy = bool(args.copy or args.stop_copy)
    wants_source = bool(args.source or args.stop_source)

    if args.list_sources:
        names = pulse.source_names()
        monitors = pulse.monitor_sources()
        if not names and not monitors:
            print("no capture sources found (only the cable itself)")
            return 0
        if names:
            print("  hardware inputs you can loop into the cable:")
            for n in names:
                print(f"    {n}")
        if monitors:
            print("  other programs' outputs (share one receiver with them):")
            for n in monitors:
                print(f"    {n}")
        print("\n  use: morse-logger route-audio --source NAME")
        return 0

    if wants_source and (wants_move or wants_copy):
        print("pick one: --source loops a hardware input; --all/--index move "
              "playback streams; --copy copies the speakers.")
        return 2

    if args.source:
        try:
            created = pulse.loop_source(args.source, state_file=sf)
        except pulse.PulseError as e:
            print(str(e))
            return 2
        loops = list(dict.fromkeys(list(cfg.get("source_loops") or [])
                                   + [args.source]))
        cfg["source_loops"] = loops
        logger.save_config(cfg)
        if created:
            print(f"line input loop on: {args.source} -> the cable")
            print("the input is still usable by other programs; nothing was moved")
        else:
            print(f"already looped: {args.source} is already feeding the cable")
        print("remembered in config.json: a restart rebuilds this loop")
        return 0

    if args.stop_source:
        killed = pulse.stop_source_loops(state_file=sf)
        cfg["source_loops"] = []
        logger.save_config(cfg)
        print(f"line input loop off ({len(killed)} module(s) unloaded)" if killed
              else "no hardware-input loop was running")
        return 0

    if wants_copy and wants_move:
        print("pick one: --copy sends a copy of your speakers to the cable, "
              "while --all/--index move individual streams.")
        return 2
    if args.copy and args.stop_copy:
        print("pick one: --copy turns the copy on, --stop-copy turns it off.")
        return 2

    if args.copy:
        if pulse.cable_status()["monitor"]:
            print("monitoring is on (cable -> your speakers), so copying your "
                  "speakers back into the cable would be a feedback loop.")
            print("pick one: run 'morse-logger cable up --no-monitor' first, "
                  "or move streams instead with 'morse-logger route-audio --all'.")
            return 2
        done = pulse.ensure_cable(state_file=sf, monitor=False, tap=True)
        if done["tap"] or pulse._tap_active():
            cfg["tap"] = True
            logger.save_config(cfg)
            print("copy on: everything your speakers play now also goes to the cable")
            print("nothing was moved, so your apps keep playing where they are")
        else:
            print("could not create the copy loop (is the cable up?)")
            return 1
        return 0

    if args.stop_copy:
        n = pulse.stop_tap(state_file=sf)
        cfg["tap"] = False
        logger.save_config(cfg)
        print(f"copy off ({len(n)} module(s) unloaded)" if n else "copy was not on")
        return 0

    if wants_move and pulse._tap_active():
        print("the copy loop is on, so streams are already being fed to the cable.")
        print("pick one: 'morse-logger route-audio --stop-copy' first if you want "
              "to move streams instead.")
        return 2

    if args.list:
        rows = pulse.sink_input_details()
        if not rows:
            print("no application audio streams are playing right now")
            return 0
        print(f"{'idx':>4}  {'sink':<28} {'app':<20} stream")
        for r in rows:
            print(f"{r['index']:>4}  {r['sink']:<28} {r['app'][:20]:<20} {r['stream']}")
        return 0
    picked = [row for row in pulse.sink_input_details()
              if (not args.index or str(row["index"]) in {str(i) for i in args.index})
              and not str(row["index"]).startswith("loopback")]
    moved, skipped = pulse.route_sink_inputs(target=pulse.sink_name(getattr(args, "to", 1) or 1),
                                             indices=args.index, state_file=sf)
    # Remember the applications in scope whether or not they had to move, so a
    # stream that is already on the cable still gets its app remembered - that is
    # what makes the routing survive a browser reload.
    apps = sorted({(row["app"] if row["app"] != "?" else row["stream"])
                   for row in picked
                   if not (row.get("stream") or "").startswith("loopback-")})
    if apps:
        cfg = logger.load_config()
        cfg["route_apps"] = sorted(set(list(cfg.get("route_apps") or []) + apps))
        logger.save_config(cfg)
        print(f"  remembered {', '.join(apps)}: new streams from these apps are "
              f"routed automatically while morse-logger runs "
              f"(morse-logger config --set route_watch=false to stop that)")
    for idx, name in moved:
        print(f"moved stream {idx} ({name}) to '{pulse.SINK}'")
    for idx, name, why in skipped:
        print(f"skipped stream {idx} ({name}) - {why}")
    if not moved:
        if any(why == "already on the cable" for _i, _n, why in skipped):
            print("everything that is playing is already on the cable")
            return 0
        print("nothing moved (is the WebSDR/browser actually playing audio?)")
        return 1
    return 0


def parse_freq(text):
    """Accept 7.03 (MHz), 7030 (kHz) or 7030000 (Hz) and return Hz."""
    v = float(text)
    if v < 1000:
        print(f"note: reading {text} as {v:g} MHz = {int(v * 1e6)} Hz")
        return int(v * 1e6)
    if v < 100000:
        print(f"note: reading {text} as {v:g} kHz = {int(v * 1e3)} Hz")
        return int(v * 1e3)
    return int(v)


# the SDR feed helpers live in feed.py now (the daemon and `input` need them
# too); these names stay because cmd_sdr and the tests already use them
sdr_pidfile = feed.sdr_pidfile
_sdr_processes = feed.sdr_processes


def cmd_sdr(args):
    cfg = logger.load_config()
    home = cfg["_home"]
    run_dir = os.path.join(home, "run")
    os.makedirs(run_dir, exist_ok=True)
    pid_path = sdr_pidfile(home)

    if args.list:
        found, raw = sdr.devices()
        if not found:
            print("no RTL-SDR found.")
            for line in (raw or "").strip().splitlines()[-4:]:
                print(f"  {line.strip()}")
            print("\nIf a stick is plugged in, the kernel DVB driver may be holding "
                  "it:\n  blacklist dvb_usb_rtl28xxu   (in /etc/modprobe.d/), then "
                  "replug")
            return 1
        print(f"{len(found)} device(s):")
        for d in found:
            print(f"  {sdr.describe(d)}")
            if d.get("serial"):
                print(f"      use: morse-logger sdr start --device {d['serial']} ...")
        return 0

    if args.stop:
        pid = None
        try:
            pid = int(open(pid_path).read().strip())
        except (OSError, ValueError):
            pid = None
        others = [(p, h) for p, h in _sdr_processes() if p != pid]
        if others:
            print(f"note: {len(others)} other SDR feed(s) running outside this home:")
            for p, h in others:
                print(f"  pid {p}" + (f"  MORSE_LOGGER_HOME={h}" if h else ""))
                try:
                    os.kill(p, signal.SIGTERM)
                    print("   stopped")
                except OSError as exc:
                    print(f"   could not stop: {exc}")
        if pid is None:
            print("sdr: not running" + (" (untracked feed(s) handled above)" if others else ""))
            return 0
        if alive(pid):
            os.kill(pid, signal.SIGTERM)
            for _ in range(40):
                if not alive(pid):
                    break
                time.sleep(0.25)
            print("sdr: stopped")
        else:
            print("sdr: not running (stale pid file)")
        try:
            os.unlink(pid_path)
        except OSError:
            pass
        return 0

    if args.status:
        pid = None
        try:
            pid = int(open(pid_path).read().strip())
        except (OSError, ValueError):
            pass
        if pid and alive(pid):
            print(f"sdr: running (pid {pid})")
            print(f"     {cfg.get('sdr_freq_hz', 0) / 1e6:g} MHz "
                  f"{cfg.get('sdr_mode', 'usb')}, device {cfg.get('sdr_device') or '#' + '0'}")
            print(f"     log: {os.path.join(run_dir, 'sdr.out')}")
        else:
            print("sdr: not running")
            for p, h in _sdr_processes():
                print(f"  but pid {p} is running morse_logger.sdr_run"
                      + (f" (MORSE_LOGGER_HOME={h})" if h else ""))
        return 0

    if not args.freq:
        print("give a frequency: morse-logger sdr start 7.03        (MHz)")
        print("                  morse-logger sdr start 144.05 --mode usb")
        print("                  morse-logger sdr --list            (find the sticks)")
        return 2

    if alive(read_pid(home)) is False and not status_json(home):
        print("note: the logger is not running - start it with 'morse-logger start' so "
              "the audio is decoded")

    freq = parse_freq(args.freq)
    mode = args.mode or cfg.get("sdr_mode", "usb")
    hf = args.hf or freq < sdr.HF_LIMIT_HZ
    if freq < sdr.HF_LIMIT_HZ and not args.hf:
        print(f"note: {freq / 1e6:g} MHz is below 24 MHz, so direct sampling is "
              f"required - only an RTL-SDR Blog V3 (or similar) can receive here")
    if freq >= sdr.HF_LIMIT_HZ and args.hf:
        print(f"note: --hf with {freq / 1e6:g} MHz - direct sampling is for HF, "
              f"ignoring it")

    for key, val in (("sdr_freq_hz", freq), ("sdr_mode", mode),
                     ("sdr_device", args.device), ("sdr_ppm", args.ppm),
                     ("sdr_gain", args.gain)):
        if val is not None:
            cfg[key] = val
    logger.save_config(cfg)

    if os.path.exists(pid_path):
        try:
            old = int(open(pid_path).read().strip())
        except ValueError:
            old = None
        if old and alive(old):
            os.kill(old, signal.SIGTERM)
            time.sleep(1.0)

    env = dict(os.environ)
    env["PYTHONPATH"] = PKG_PARENT + (
        ":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONUNBUFFERED"] = "1"
    logf = open(os.path.join(run_dir, "sdr.out"), "a")
    cmd = [sys.executable, "-m", "morse_logger.sdr_run", "--freq", str(freq),
           "--mode", mode, "--ppm", str(args.ppm),
           "--sample-rate", str(args.sample_rate)]
    if args.device:
        cmd += ["--device", args.device]
    if args.gain not in (None, ""):
        cmd += ["--gain", str(args.gain)]
    if hf:
        cmd += ["--hf"]
    if args.bias_tee:
        cmd += ["--bias-tee"]
    p = subprocess.Popen(cmd, stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
                         start_new_session=True, env=env)
    with open(pid_path, "w") as f:
        f.write(str(p.pid))
    time.sleep(1.5)
    if p.poll() is not None:
        print(f"sdr: exited immediately (code {p.returncode}); see "
              f"{os.path.join(run_dir, 'sdr.out')}")
        for line in open(os.path.join(run_dir, "sdr.out")).read().splitlines()[-6:]:
            print("  " + line)
        return 1
    print(f"sdr: running (pid {p.pid}) on {freq / 1e6:g} MHz {mode.upper()}"
          f"{' with HF direct sampling' if hf else ''}")
    print(f"  audio -> {pulse.SINK}; {cfg.get('tone_hz')} Hz decoder tone")
    print("  next: morse-logger tune --auto      (find the note in the passband)")
    print("        morse-logger wpm             (measure the fist)")
    return 0


def _daemon_pid(home):
    path = os.path.join(home, "run", "morse-logger.pid")
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def apply_live(home, want_wpm=None, timeout=45.0):
    """Ask a running logger to apply config.json. Returns (applied, note)."""
    pid = _daemon_pid(home)
    if not pid:
        return False, "not running - the new speed applies next time you start"
    try:
        os.kill(pid, signal.SIGUSR1)
    except OSError as e:
        return False, f"could not signal the logger ({e}); restart it yourself"
    deadline = time.time() + timeout
    status = os.path.join(home, "run", "status.json")
    while time.time() < deadline:
        try:
            with open(status) as f:
                st = json.load(f)
        except (OSError, ValueError):
            st = {}
        fl = st.get("fldigi") or {}
        if fl.get("running") and (want_wpm is None or st.get("wpm") == want_wpm):
            return True, f"applied live: decoder now at {st.get('wpm')} WPM"
        time.sleep(0.5)
    return False, "the logger did not confirm in time; check 'morse-logger status'"


def cmd_wpm(args):
    """Measure the sender's keying speed, or set it."""
    cfg = logger.load_config()
    if args.set is not None:
        cfg["wpm"] = float(args.set)
        logger.save_config(cfg)
        print(f"decoder speed set to {args.set:g} WPM")
        ok, note = apply_live(cfg["_home"], want_wpm=float(args.set))
        print("  " + note)
        return 0 if ok or not _daemon_pid(cfg["_home"]) else 1
    if not args.auto and args.set is None and args.measure is False:
        print(f"configured decoder speed: {cfg['wpm']:g} WPM")
    tone = args.tone if args.tone is not None else cfg.get("tone_hz")
    m = wpm.measure_live(
        lambda sec: pulse.capture_samples(sec, rate=8000, source=cfg["input_device"]),
        tone_hz=tone, windows=(args.seconds, 10, 15),
        say=lambda s: print(s))
    if m.get("attempts", 1) > 1:
        print(f"  (took {m['attempts']} windows, {m['listened_s']:.0f} s: the first was "
              f"not clean CW - the band is fading or something burst into the passband)")
    if not m:
        print("could not measure: no keyed CW found in the last few seconds "
              "(tune a signal in first)")
        return 1
    if "rejected" in m:
        print(f"could not measure: {m['rejected']}")
        return 1
    print(f"measured {m['wpm']:g} WPM  (dot {m['dot_ms']:g} ms, "
          f"{m['marks']} marks, dash ratio {m['dash_ratio']}, "
          f"confidence {m['confidence']:.2f}; keying measured at "
          f"{tone:g} Hz)")
    if m["confidence"] < 0.3:
        print("  low confidence - the signal is noisy or the fist is irregular; "
              "treat the number as a hint only")
    if args.auto:
        cfg["wpm"] = round(m["wpm"])
        logger.save_config(cfg)
        print(f"decoder speed set to {cfg['wpm']:g} WPM")
        ok, note = apply_live(cfg["_home"], want_wpm=float(cfg["wpm"]))
        print("  " + note)
        if not ok and _daemon_pid(cfg["_home"]):
            return 1
    else:
        print(f"  current setting is {cfg['wpm']:g} WPM; "
              f"use --auto to save the measurement")
    return 0


def cmd_translate(args):
    """Try the abbreviation glossary on arbitrary text."""
    text = " ".join(args.text) if args.text else ""
    if not text:
        if sys.stdin.isatty():
            print("give some text: morse-logger translate \"CQ CQ DE K4EIE K\"")
            return 2
        text = sys.stdin.read().strip()
    mode = args.mode or logger.load_config().get("translate", "gloss")
    marks = logger.load_config().get("translate_uncertain", True)
    if getattr(args, "no_marks", False):
        marks = False
    glossed, n = translate.translate(text, "gloss", mark_uncertain=marks)
    print(f"raw        {text}")
    print(f"coherence  {translate.coherence(text):.2f}"
          "   (fraction of tokens that look like real words/callsigns/reports)")
    print(f"glossed    {glossed}   ({n} terms)")
    if translate.coherence(text) < 0.45:
        print("           below gloss_min_coherence: the daemon would store this raw, "
              "unannotated")
    if args.all_modes:
        replaced, _ = translate.translate(text, "replace")
        print(f"replaced   {replaced}")
        print(f"\n{translate.legend()}")
    return 0


def cmd_dial(args):
    """Set (or show) the dial frequency stamped on new log lines, live."""
    cfg = logger.load_config()
    home = cfg["_home"]
    if args.mhz is None and not args.clear:
        live = logger.read_dial(cfg.get("dial_mhz"), home)
        shown = f"{live:g} MHz" if live else "not set"
        print(f"dial: {shown}")
        if not alive(read_pid(home)):
            print("(logger is not running; this is the value it will use)")
        return 0
    mhz = None if args.clear else args.mhz
    if mhz is not None and mhz >= 100:
        print(f"note: {mhz:g} looks like kHz - recording {mhz / 1000:g} MHz")
        mhz = mhz / 1000
    logger.write_dial(mhz, home)
    cfg["dial_mhz"] = mhz
    logger.save_config(cfg)          # also the default for the next start
    if mhz is None:
        print("dial cleared - new lines will carry no frequency")
    else:
        stamp = "running" if alive(read_pid(home)) else "not running (applied to next start)"
        print(f"dial set to {mhz:g} MHz - applies to new lines immediately ({stamp})")
    return 0



def daemon_pids(home):
    """Every morse-logger daemon using this data directory (not just the pid file).

    A second daemon overwrites the pid file and the first becomes untracked, so
    stopping "the pid" left one running: both then fight over the XML-RPC port
    and neither logs anything.
    """
    out = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            raw = open(f"/proc/{name}/cmdline", "rb").read().split(b"\0")
        except OSError:
            continue
        args = [a.decode("utf-8", "replace") for a in raw if a]
        if not any(a.endswith("morse_logger.cli") or a == "morse_logger.cli" for a in args):
            continue
        if "run" not in args:
            continue
        env_home = None
        try:
            env = open(f"/proc/{name}/environ", "rb").read().decode("utf-8", "replace")
            for item in env.split("\0"):
                if item.startswith("MORSE_LOGGER_HOME="):
                    env_home = item.split("=", 1)[1]
        except OSError:
            pass
        if env_home and os.path.abspath(env_home) != os.path.abspath(home):
            continue
        out.append(int(name))
    return out


def cmd_scan(args):
    from . import scan as scanner
    if args.list_bands:
        print("  CW segments that ship with the tool:")
        for name, (lo, hi) in sorted(scanner.BANDS.items()):
            print(f"    {name:<4} {lo/1e6:7.3f} - {hi/1e6:7.3f} MHz")
        print("\n  any other range: --start 7030 --stop 7060   (kHz)")
        return 0
    try:
        start, stop, label = scanner.band_range(args.band, args.start, args.stop)
    except ValueError as exc:
        print(f"  {exc}")
        return 2
    print(f"  {label}")
    try:
        results = scanner.scan(
            band=args.band, start_khz=args.start, stop_khz=args.stop,
            top=args.top, dwell=args.dwell, seconds=args.survey_seconds,
            device=args.device, ppm=args.ppm, gain=args.gain, hf=args.hf,
            bias_tee=args.bias_tee, mode=args.mode,
            verbose=lambda msg: print(f"  {msg}"))
    except (RuntimeError, ValueError) as exc:
        print(f"  {exc}")
        return 1
    scanner.report(results)
    return 0

def cmd_start(args):
    holder = logger.lock_holder()
    if holder:
        print(f"already running: another daemon holds the lock (pid {holder})")
        print("  use 'morse-logger restart' to replace it, or 'morse-logger stop' first")
        return 3
    cfg = logger.load_config()
    home = cfg["_home"]
    if alive(read_pid(home)):
        print(f"already running (pid {read_pid(home)}). Use 'morse-logger stop' first.")
        return 1
    if args.dial is not None and args.dial >= 100:
        print(f"note: --dial {args.dial:g} looks like kHz - using {args.dial / 1000:g} MHz")
        args.dial = args.dial / 1000
    if args.dial is not None:
        cfg["dial_mhz"] = args.dial
        logger.save_config(cfg)
        logger.write_dial(args.dial, home)
    if args.tone is not None:
        cfg["tone_hz"] = args.tone
        logger.save_config(cfg)
    if args.no_monitor:
        cfg["monitor"] = False
        logger.save_config(cfg)
    if args.monitor:
        cfg["monitor"] = True
        logger.save_config(cfg)
    os.makedirs(os.path.join(home, "run"), exist_ok=True)
    logf = open(os.path.join(home, "run", "morse-logger.out"), "a")
    env = dict(os.environ)
    env["PYTHONPATH"] = PKG_PARENT + (":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONUNBUFFERED"] = "1"
    p = subprocess.Popen([sys.executable, "-m", "morse_logger.cli", "run"],
                         stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
                         start_new_session=True, env=env)
    with open(pidfile(home), "w") as f:
        f.write(str(p.pid))
    t0 = time.time()
    last_phase = None
    deadline = t0 + 60
    while time.time() < deadline:
        if p.poll() is not None:
            print(f"logger exited during startup (code {p.returncode})")
            try:
                with open(os.path.join(home, "run", "morse-logger.out"), errors="replace") as f:
                    print("last lines of the log:\n" + "".join(f.readlines()[-12:]).rstrip())
            except OSError:
                pass
            return 1
        st = status_json(home)
        phase = (st or {}).get("phase")
        if phase and phase != last_phase:
            print(f"  ... {phase} ({int(time.time() - t0)}s)")
            last_phase = phase
        if st and st.get("fldigi", {}).get("running") \
                and st.get("phase") in (None, "logging"):
            print(f"morse-logger running (pid {p.pid})")
            print(f"  decoder: fldigi CW, tone {st['fldigi'].get('tone_hz')} Hz, "
                  f"input '{st['input_device']}'")
            print(f"  logs:    {st['log_dir']}")
            print(f"\nPoint the audio at the cable, e.g.:")
            print(f"  morse-logger route-audio --all  (moves the WebSDR/browser tab onto the cable)")
            return 0
        time.sleep(0.5)
    tail_lines = ""
    try:
        with open(os.path.join(home, "run", "morse-logger.out"), errors="replace") as f:
            tail_lines = "".join(f.readlines()[-12:])
    except OSError:
        pass
    if tail_lines:
        print("last lines of the log:\n" + tail_lines.rstrip())
    print("logger did not report ready in 60s; full log: "
          f"{os.path.join(home, 'run', 'morse-logger.out')}")
    return 1


def cmd_run(args):
    cfg = logger.load_config()
    os.makedirs(os.path.join(cfg["_home"], "run"), exist_ok=True)
    fldigi.stop_stale(cfg["_home"])
    from .daemon import Runner
    Runner(cfg).run()
    return 0


def cmd_restart(args):
    """Stop the logger and start it again, keeping the dial frequency."""
    home = logger.load_config()["_home"]
    pid = _daemon_pid(home)
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        deadline = time.time() + 20
        while time.time() < deadline and _daemon_pid(home):
            time.sleep(0.3)
        if _daemon_pid(home):
            print("the logger ignored SIGTERM; killing it")
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
            time.sleep(0.5)
            for name in ("morse-logger.pid", "status.json"):
                try:
                    os.unlink(os.path.join(home, "run", name))
                except OSError:
                    pass
    print("stopped")
    return cmd_start(args)


def cmd_stop(args):
    cfg = logger.load_config()
    home = cfg["_home"]
    # every daemon for this home, not just the one named in the pid file: a
    # second instance overwrites that file and keeps running untracked
    pids = [p for p in {read_pid(home), *daemon_pids(home)} if alive(p)]
    if not pids:
        print("morse-logger is not running")
        fldigi.stop_stale(home)
        return 0
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.time() + 15
    while time.time() < deadline and any(alive(p) for p in pids):
        time.sleep(0.3)
    left = [p for p in pids if alive(p)]
    if left:
        for pid in left:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        print("forced stop")
    if len(pids) > 1:
        print(f"stopped {len(pids)} daemons (a second instance was running)")
    try:
        os.unlink(pidfile(home))
    except OSError:
        pass
    print("morse-logger stopped")
    return 0


def cmd_status(args):
    cfg = logger.load_config(create=False)
    st = status_json(cfg["_home"])
    pid = read_pid(cfg["_home"])
    if args.json:
        print(json.dumps(st or {"running": False}, indent=2))
        return 0
    if not alive(pid) or not st:
        # Inside a sandbox (Flatpak, container) the daemon runs in a different pid
        # namespace, so its pid means nothing here. A status.json written moments
        # ago is better evidence than a pid we cannot see.
        path = os.path.join(cfg["_home"], "run", "status.json")
        age = None
        try:
            age = time.time() - os.path.getmtime(path)
        except OSError:
            pass
        if st and age is not None and age < 20:
            print(f"note: the daemon's pid is not visible in this namespace; "
                  f"status.json is {age:.0f}s old, so it is running")
        else:
            print("morse-logger is not running")
            return 1
    fl = st.get("fldigi", {})
    print(f"morse-logger {__version__} - RUNNING  pid {st['pid']}  uptime {fmt_uptime(st['uptime_s'])}")
    speed = st.get("wpm")
    meas, ago = st.get("wpm_measured"), st.get("wpm_measured_ago")
    note = ""
    if meas and (speed is None or abs(meas - speed) >= 0.5):
        note = f"  (last measured {meas:g} WPM {ago}s ago)"
    health = "OK" if fl.get("running") else "NOT RESPONDING"
    print(f"  service   fldigi pid {fl.get('pid')}  {health}")
    print(f"  settings  tone {fl.get('tone_hz')} Hz  speed {speed:g} WPM{note}")
    q = fl.get("quality")
    print(f"  signal    quality {q:.1f}" if isinstance(q, (int, float))
          else "  signal    quality -")
    if st.get("restarts", 0) > 5:
        print(f"  problem   decoder restarted {st['restarts']}x - nothing is being "
              f"logged; see run/morse-logger.out")
    print(f"  input     {st.get('input_device')}")
    try:
        print(f"  feed      {feed.describe(cfg)[0]}")
    except Exception as exc:                       # never fail status on this
        print(f"  feed      unknown ({exc})")
    if st.get("dial_mhz"):
        print(f"  dial      {st['dial_mhz']:g} MHz")
    print(f"  cable     sink={st['cable']['sink']} source={st['cable']['source']} "
          f"monitor={st['cable']['monitor']}")
    prof = cfg.get("_cpu_profile") or cfg.get("cpu_profile") or "auto"
    score = cfg.get("_cpu_score")
    detail = f"measured {score:.3f}s" if score is not None else "set in config"
    print(f"  cpu       profile {prof} ({detail}), speed check every "
          f"{logger.scaled(cfg, 'speed_watchdog_interval_s', 0):.0f}s")
    if cfg.get("translate", "gloss") != "off":
        print(f"  glossary  {cfg['translate']} (original tokens kept in [brackets])")
    if st.get("speed_restarts"):
        print(f"  speed wd  {st['speed_restarts']} speed correction(s), "
              f"decoder now {st.get('wpm')} WPM")
    if st.get("watchdog_retunes"):
        print(f"  watchdog  {st['watchdog_retunes']} automatic retune(s) this session")
    if st.get("last_repair"):
        print(f"  repair    {st['last_repair']}")
    print(f"  logged    {st.get('lines')} lines  (restarts {st.get('restarts')})")
    if st.get("last_line"):
        nums = []
        if st.get("last_quality") is not None:
            nums.append(f"q={st['last_quality']}")
        if st.get("last_coherence") is not None:
            nums.append(f"coherence={st['last_coherence']}")
        print(f"  last      {st['last_line']}" + (f"  ({', '.join(nums)})" if nums else ""))
    print(f"  logs      {st['log_dir']}")
    return 0


def cmd_input(args):
    """Show what is feeding the decoder right now (read-only).

    Distinct from `status`: status says whether the process is healthy, this
    says which audio is arriving - the question behind a log that stays empty.
    """
    cfg = logger.load_config(create=False)
    try:
        summary, detail = feed.describe(cfg)
    except Exception as exc:                     # no sound server, no pactl
        print(f"cannot inspect the audio setup: {exc}")
        return 2
    print(f"input    {summary}")
    for line in detail:
        print(line)
    return 0


def pct_arg(value, name):
    """Accept 45, 45.0 or 0.45 for a 0-100 score and normalise it."""
    if value is None:
        return None
    v = float(value)
    if v > 100:
        raise SystemExit(f"--{name} {value:g} is above 100: q= and coherence both use 0-100")
    if 0 < v <= 1:
        print(f"note: reading --{name} {value:g} as {v * 100:g} "
              f"(both scores are 0-100, like q=45)", file=sys.stderr)
        v *= 100
    return v


_STOP = {"now": False}


def _install_stop_handlers():
    """Ctrl-C and SIGTERM both end the follow loop (a background job started by
    a non-interactive shell inherits SIGINT ignored, so SIGTERM matters)."""
    def _stop(_sig, _frm):
        _STOP["now"] = True
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):
            pass


def _rec_passes(rec, args):
    """Shared filter for the JSONL view and its follow mode."""
    if args.min_quality is not None and (rec.get("quality") or 0) < args.min_quality:
        return False
    if args.min_coherence is not None:
        coh = rec.get("coherence")
        if coh is None:                       # predates the field
            coh = round(translate.coherence(rec.get("text", "")) * 100)
            rec["_coh_computed"] = True
        if coh < args.min_coherence:
            return False
    if args.min_chars is not None and len(rec.get("text") or "") < args.min_chars:
        return False
    if args.hide_filtered and rec.get("filtered"):
        return False
    return True


def _show_rec(rec, args):
    when = (rec.get("logged_at") or "")[:19].replace("T", " ")
    q = rec.get("quality")
    c = rec.get("coherence")
    if c is None and rec.get("_coh_computed"):
        c = round(translate.coherence(rec.get("text", "")) * 100)
    tags = f"q={q:g}" if q is not None else "q=?"
    if c is not None:
        tags += f" coh={c:g}" if isinstance(c, float) else f" coh={c}"
    text = rec.get("text") if args.raw else (rec.get("text_xlated") or rec.get("text"))
    if getattr(args, "fldigi_debug", False):
        # same timestamp and the same bracket width, so fldigi's characters
        # begin in the same column as the rendered text on the line below
        raw = (rec.get("text") or "").replace("\n", " ")
        label = f"{'fldigi':<{max(len(tags), 6)}}"
        print(f"{when}  [{label}]  {raw}", flush=True)
    print(f"{when}  [{tags}]  {text}", flush=True)


def _follow_lines(path_fn, poll=0.5, start_at_end=True):
    """Yield complete lines as they are appended, byte-accurately.

    Handles the four ways a naive `tail -f` on our logs goes quiet forever:

    * **date rollover** - the daemon starts morse-<new-day>.txt at midnight, so the
      filename must be re-resolved every poll, not captured at startup;
    * **half-written line** - a read that lands mid-line is held back in `tail`
      instead of being printed mangled and then lost;
    * **truncation or replacement** - a smaller size or a different inode means
      start again from the beginning;
    * **split multibyte characters** - positions are byte offsets from a binary
      handle, never text-mode cookies.
    """
    path = path_fn()
    ino, pos, tail = None, 0, b""
    try:
        st = os.stat(path)
        ino, pos = st.st_ino, (st.st_size if start_at_end else 0)
    except OSError:
        pass
    while not _STOP["now"]:
        p = path_fn()
        try:
            st = os.stat(p)
        except OSError:
            # file not there yet (first record of a new day)
            path, ino, pos, tail = p, None, 0, b""
            time.sleep(poll)
            continue
        if p != path or st.st_ino != ino:
            path, ino, pos, tail = p, st.st_ino, 0, b""     # new day / new file
        elif st.st_size < pos:
            pos, tail = 0, b""                              # truncated
        if st.st_size > pos:
            try:
                with open(p, "rb") as f:
                    f.seek(pos)
                    chunk = f.read()
                    pos = f.tell()
            except OSError:
                time.sleep(poll)
                continue
            data = tail + chunk
            cut = data.rfind(b"\n")
            if cut >= 0:
                tail = data[cut + 1:]
                for line in data[:cut + 1].splitlines():
                    if line:
                        yield line.decode("utf-8", "replace")
            else:
                tail = data                                  # incomplete line
        time.sleep(poll)


def _tail_path(logdir, date=None):
    def fn():
        d = date or time.strftime("%Y-%m-%d")
        return os.path.join(logdir, f"morse-{d}.txt")
    return fn


def _jsonl_path(logdir):
    return lambda: os.path.join(logdir, "morse.jsonl")


def cmd_tail(args):
    cfg = logger.load_config(create=False)
    logdir = cfg["log_dir"]
    day = args.date or time.strftime("%Y-%m-%d")
    args.min_quality = pct_arg(args.min_quality, "min-quality")
    args.min_coherence = pct_arg(args.min_coherence, "min-coherence")
    by_jsonl = args.raw or args.fldigi_debug or args.min_quality is not None \
        or args.min_coherence is not None or args.min_chars is not None

    if by_jsonl:
        path = _jsonl_path(logdir)()
        if not os.path.exists(path) and not args.follow:
            print(f"(no log yet: {path})")
            return 1
        rows = []
        if os.path.exists(path):
            with open(path, errors="replace") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if (rec.get("logged_at") or "")[:10] == day and _rec_passes(rec, args):
                        rows.append(rec)
        sure = sum(1 for r in rows if r.get("_coh_computed"))
        if sure:
            print(f"note: {sure} of {len(rows)} lines predate the coherence field - "
                  f"scores computed from the text", file=sys.stderr)
        for rec in rows[-args.lines:]:
            _show_rec(rec, args)
        if not args.follow:
            return 0
        _install_stop_handlers()
        for line in _follow_lines(_jsonl_path(logdir), start_at_end=True):
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if args.date and (rec.get("logged_at") or "")[:10] != args.date:
                continue
            if _rec_passes(rec, args):
                _show_rec(rec, args)
        return 0

    path = _tail_path(logdir, args.date)()
    if os.path.exists(path):
        with open(path, errors="replace") as f:
            lines = f.readlines()
        sys.stdout.write("".join(lines[-args.lines:]))
        sys.stdout.flush()
    elif not args.follow:
        print(f"(no log for {day} yet: {path})")
        return 1
    if args.follow:
        _install_stop_handlers()
        for line in _follow_lines(_tail_path(logdir, args.date), start_at_end=True):
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
    return 0


def cmd_tune(args):
    cfg = logger.load_config()
    rpc = fldigi.Rpc(cfg["xmlrpc_port"])
    try:
        rpc.call("fldigi.name")
    except Exception:
        print("decoder is not running (start it with: morse-logger start)")
        return 1
    if args.auto:
        # CW arrives in bursts: a four second window can easily land between
        # overs, so keep listening before declaring the band empty.
        data, best = b"", None
        for window in (args.seconds, 6.0, 8.0):
            data += pulse.capture_samples(window, rate=8000, source=cfg["input_device"])
            if len(data) < 8000:                   # less than half a second
                print("could not read audio from the cable - check it with "
                      "'morse-logger cable status' and 'morse-logger route-audio --list'")
                return 1
            lvl = autotune.level(data)
            if lvl < 20:
                print(f"the cable is silent (level {lvl:.0f}) - nothing is playing into it; "
                      "see 'morse-logger route-audio --list'")
                return 1
            best = autotune.choose(data)
            if best.get("usable") and best.get("tone_hz"):
                if window != args.seconds:
                    print(f"  locked after {len(data) / 16000:.0f}s of listening")
                break
            print(f"  nothing lockable in {len(data) / 16000:.0f}s of audio - listening longer")
        if not best.get("usable"):
            why = "clipped at 0 dBFS - lower the volume" if best.get("clipping") else \
                  "no steady carrier and no keyed tone found"
            near = f" (strongest {best['tone_hz']} Hz, steady {best.get('stability') or 0:.0%})" \
                   if best.get("tone_hz") else ""
            print(f"no CW tone to lock onto: {why}{near}")
            print(f"  the cable has audio (level {lvl:.0f}); the signal may be fading, "
                  f"or several signals may share the filter")
            return 1
        print(f"strongest steady tone: {best['tone_hz']} Hz "
              f"(steady in {int(best['stability'] * 100)}% of windows)")
        if best.get("clipping"):
            print("the input is clipped at 0 dBFS - lower the source volume, and check "
                  "for a feedback loop with 'morse-logger route-audio --list' (a loopback stream on "
                  "the cable feeds itself).")
            return 1
        if not best["usable"]:
            print("no steady CW carrier detected - tune the receiver so the CW note "
                  "is clearly audible, then try again")
            return 1
        rpc.set_carrier(best["tone_hz"])
        hz, q, _tried = fldigi.refine_carrier(rpc, best["tone_hz"])
        cfg["tone_hz"] = hz
        logger.save_config(cfg)
        print(f"decoder tone set to {hz} Hz (decoder quality {q})")
        return 0
    if args.tone is None:
        print(f"current decoder tone: {rpc.carrier()} Hz")
        return 0
    rpc.set_carrier(args.tone)
    cfg["tone_hz"] = args.tone
    logger.save_config(cfg)
    print(f"decoder tone set to {args.tone} Hz")
    return 0


def _raw_audio(path):
    """Any audio file -> s16le mono 8 kHz. Uses ffmpeg for anything not raw."""
    if path.endswith(".raw"):
        with open(path, "rb") as f:
            return f.read()
    if not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg is needed to read that file; install it or convert to .raw first")
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", "8000",
                        "-f", "s16le", "-"], capture_output=True)
    if p.returncode != 0:
        raise SystemExit(f"ffmpeg could not read {path}: {p.stderr.decode()[:200]}")
    return p.stdout


def cmd_retime(args):
    """Re-time Farnsworth-spaced CW (W1AW 5 WPM practice) to standard spacing."""
    from . import retime as rt, sdr
    cfg = logger.load_config()
    path = args.file
    if not os.path.exists(path):
        print(f"no such file: {path}")
        return 1
    tone = args.tone if args.tone is not None else (cfg.get("tone_hz") or 750)
    dot = (1.2 / args.wpm) if args.wpm else None
    try:
        raw = _raw_audio(path)
    except SystemExit as exc:
        print(exc)
        return 1
    pcm, st = rt.retime(raw, 8000, float(tone), dot_s=dot)
    if not pcm:
        print(f"could not retime: {st.get('error', 'no keying found')}")
        return 1
    print(f"tone {tone:g} Hz, character speed "
          f"{(1.2 / (st['dot_ms'] / 1000)):.1f} WPM "
          f"(dot {st['dot_ms']:g} ms)")
    print(f"  marks {st['marks']} ({st['dots']} dots, {st['dashes']} dashes), "
          f"{st['letters']} letter gaps, {st['words']} word gaps")
    print(f"  {st['in_seconds']:g}s in -> {st['out_seconds']:g}s out")
    out = args.out or (os.path.splitext(path)[0] + ".retimed.raw")
    with open(out, "wb") as f:
        f.write(pcm)
    print(f"  wrote {out}")
    if not args.play:
        print("  play it into the cable with: morse-logger retime "
              f"{os.path.basename(path)} --play   (or feed the .raw yourself)")
        return 0
    pulse.ensure_cable(state_file=os.path.join(cfg["_home"], "run", "pulse-modules.json"),
                       monitor=cfg.get("monitor", True))
    cmd = sdr.player_cmd(pulse.SINK)
    print("  playing into the cable - Ctrl-C to stop")
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    try:
        proc.communicate(pcm)
    except KeyboardInterrupt:
        proc.terminate()
    return 0


def cmd_selftest(args):
    cfg = logger.load_config()
    if alive(read_pid(cfg["_home"])):
        print("note: logger is running; the self test needs the decoder to itself.")
        print("      run 'morse-logger stop' first if the test fails to start fldigi.")
    report, ok = selftest.run(cfg, wpm=args.wpm, noise=args.noise, tone=args.tone,
                              keep=args.keep)
    print("\n" + ("SELF TEST PASSED" if ok else "SELF TEST FAILED"))
    return 0 if ok else 1


def cmd_config(args):
    cfg = logger.load_config()
    if args.set:
        key, _, val = args.set.partition("=")
        if key not in logger.DEFAULT_CONFIG:
            print(f"unknown key '{key}'. Keys: {', '.join(sorted(logger.DEFAULT_CONFIG))}")
            return 1
        old = cfg.get(key)
        try:
            parsed = json.loads(val)
        except Exception:
            parsed = val
        if isinstance(old, bool) and isinstance(parsed, str):
            parsed = parsed.lower() in ("1", "true", "yes", "on")
        elif isinstance(old, int) and not isinstance(parsed, bool):
            parsed = int(parsed)
        elif isinstance(old, float) and not isinstance(parsed, bool):
            parsed = float(parsed)
        if key in ("min_quality", "min_coherence", "gloss_min_coherence") \
                and isinstance(parsed, (int, float)) and 0 < parsed <= 1:
            print(f"note: reading {key}={parsed} as {parsed * 100:g} (scores are 0-100)")
            parsed = parsed * 100
        cfg[key] = parsed
        path = logger.save_config(cfg)
        print(f"{key} = {parsed!r}  (saved to {path})")
        return 0
    print(json.dumps({k: v for k, v in cfg.items() if not k.startswith("_")}, indent=2))
    print(f"\nconfig file: {os.path.join(cfg['_home'], 'config.json')}")
    return 0


def _at_full_hint(cfg):
    """What to turn up when the cable source is already at 100%.

    An RTL-SDR feed writes the cable digitally, so there is no application
    volume to raise - the knob is rtl_fm's tuner gain, passed at launch.
    """
    home = cfg.get("_home")
    if home and feed.sdr_running(home):
        return ("the cable is fed by rtl_fm, not an application - raise the "
                "tuner gain: 'morse-logger sdr start <MHz> --gain <dB>'")
    return None


def cmd_level(args):
    """Measure the cable's input level and trim the source volume toward target."""
    cfg = logger.load_config(create=False)
    if getattr(args, "auto", False) or getattr(args, "no_auto", False):
        cfg["level_auto"] = bool(args.auto)
        logger.save_config(cfg)
        print(f"level auto-trim {'on' if args.auto else 'off'}"
              + (f" - the daemon re-checks every "
                 f"{logger.scaled(cfg, 'level_auto_interval_s', 20):.0f}s"
                 if args.auto else ""))
        if not args.set:
            return 0
    target = args.target if args.target is not None else float(
        cfg.get("level_target_db", -14.0))
    step_db = float(cfg.get("level_max_step_db", 6.0))
    src = cfg.get("input_device") or pulse.SOURCE
    if not pulse.source_exists(src):
        print(f"no capture source {src} - run 'morse-logger cable up' first")
        return 2
    sample = pulse.capture_samples(args.seconds, rate=8000, source=src)
    if not sample:
        print(f"could not capture audio from {src}")
        return 2
    db = pulse.peak_dbfs(sample)
    vol = pulse.source_volume(src)
    sdr_feed = feed.sdr_running(cfg["_home"])
    verdict = pulse.level_verdict(db, sdr=sdr_feed)
    if vol is None:
        print(f"level: {db} dBFS  volume unknown  target {target:g} dBFS  [{verdict}]")
    else:
        print(f"level: {db} dBFS  volume {vol:.0f}%  target {target:g} dBFS  [{verdict}]")
    if db <= -80:
        print("  digital silence - the source is delivering zeros; fix the "
              "source, do not raise the volume")
        return 0
    if verdict == "no signal in the passband":
        print("  that is an rtl_fm feed's noise floor, not a volume problem - "
              "rtl_gain sets it, and it moves by a few dB, not fourteen")
        return 0
    plan = pulse.trim_volume(db, vol, target_db=target, max_step_db=step_db,
                             at_full_hint=_at_full_hint(cfg))
    if plan["action"] == "none":
        print(f"  {plan['reason']}")
        return 0
    print(f"  {plan['action']} by {abs(plan['delta_db']):.1f} dB -> "
          f"{plan['percent']:.0f}%")
    if args.dry_run:
        print("  dry run - nothing was changed")
        return 0
    if not args.set:
        print("  nothing changed - add --set to apply it")
        return 0
    if not pulse.set_source_volume(src, plan["percent"]):
        print(f"  could not set the volume on {src}")
        return 2
    print(f"  volume set to {plan['percent']:.0f}% - re-run 'level' to confirm")
    return 0


def cmd_logs(args):
    cfg = logger.load_config(create=False)
    print(cfg["log_dir"])
    if os.path.isdir(cfg["log_dir"]):
        for f in sorted(os.listdir(cfg["log_dir"])):
            p = os.path.join(cfg["log_dir"], f)
            print(f"  {f:<28} {os.path.getsize(p):>9} bytes")
    return 0


def cmd_prune(args):
    """Archive (or delete) logs older than N days. Off unless asked."""
    cfg = logger.load_config(create=False)
    days = args.days if args.days is not None else int(cfg.get("retention_days") or 0)
    if days <= 0:
        print("retention is off: pass --days N, or set retention_days in config.json")
        return 2
    home = cfg["_home"]
    pid = read_pid(home)
    if not args.force and (logger.lock_holder(home) or (pid and alive(pid))):
        print("the logger is running; stop it first, or use --force "
              "(a running daemon holds the JSONL open)")
        return 2
    result = retention.prune(home, cfg["log_dir"], days,
                             dry_run=args.dry_run, delete=args.delete)
    print(retention.summary_text(result))
    return 0


# ---------------------------------------------------------------- parser
def build_parser():
    p = argparse.ArgumentParser(
        prog="morse-logger",
        description="Morse (CW) logger: virtual audio cable -> fldigi CW decoder -> timestamped logs.")
    p.add_argument("--version", action="version", version=f"morse-logger {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("doctor", help="check the whole installation")
    d.set_defaults(func=cmd_doctor)

    c = sub.add_parser("cable", help="create/remove/inspect the virtual audio cable")
    c.add_argument("action", choices=["up", "down", "status"])
    c.add_argument("--monitor", action="store_true",
                   help="hear the cable on your speakers")
    c.add_argument("--no-monitor", action="store_true",
                   help="do not loop the cable output back to your speakers")
    c.add_argument("--channels", type=int, default=1, metavar="N",
                   help="how many independent cables to create (default 1; "
                        "one per simultaneous conversation)")
    c.set_defaults(func=cmd_cable)

    r = sub.add_parser("route-audio",
                       help="send playing audio (e.g. a WebSDR tab) to the cable")
    r.add_argument("--all", action="store_true",
                   help="move every playing stream (usual choice for a WebSDR)")
    r.add_argument("--to", type=int, default=1, metavar="N",
                   help="cable number to move streams onto (default 1; "
                        "create extras with 'cable up --channels N')")
    r.add_argument("--copy", action="store_true",
                   help="copy everything your speakers play into the cable "
                        "(nothing is moved, nothing needs remembering)")
    r.add_argument("--stop-copy", action="store_true",
                   help="remove that copy loop")
    r.add_argument("--source", metavar="NAME",
                   help="loop a hardware input (line in, aux, USB) into the cable")
    r.add_argument("--list-sources", action="store_true",
                   help="list hardware inputs that can be looped in")
    r.add_argument("--stop-source", action="store_true",
                   help="remove the hardware-input loop")
    r.add_argument("--index", type=int, action="append",
                   help="move one stream by index (see --list)")
    r.add_argument("--list", action="store_true", help="list playing streams")
    r.set_defaults(func=cmd_route)

    s = sub.add_parser("start", help="start logging in the background")
    s.add_argument("--dial", type=float, help="dial frequency in MHz to record with each line")
    s.add_argument("--tone", type=float, help="decoder CW tone in Hz (default 1500)")
    s.add_argument("--monitor", action="store_true", help="hear the cable on your speakers")
    s.add_argument("--no-monitor", action="store_true", help="silence monitoring")
    s.set_defaults(func=cmd_start)

    rs = sub.add_parser("restart", help="stop and start again, keeping the dial")
    rs.add_argument("--dial", type=float, help="dial frequency in MHz")
    rs.add_argument("--tone", type=float, help="decoder CW tone in Hz")
    rs.add_argument("--monitor", action="store_true", help="hear the cable on your speakers")
    rs.add_argument("--no-monitor", action="store_true", help="silence monitoring")
    rs.set_defaults(func=cmd_restart)

    run = sub.add_parser("run", help="run in the foreground (used by 'start')")
    run.set_defaults(func=cmd_run)

    st = sub.add_parser("stop", help="stop logging")
    st.set_defaults(func=cmd_stop)

    sta = sub.add_parser("status", help="show logger status")
    sta.add_argument("--json", action="store_true")
    sta.set_defaults(func=cmd_status)

    inp = sub.add_parser("input", help="what is feeding the decoder, right now")
    inp.set_defaults(func=cmd_input)

    t = sub.add_parser("tail", help="show today's log")
    t.add_argument("-n", "--lines", type=int, default=20)
    t.add_argument("-f", "--follow", action="store_true")
    t.add_argument("--date", help="YYYY-MM-DD")
    t.add_argument("--raw", action="store_true",
                   help="show the untouched decode instead of the glossed text")
    t.add_argument("--fldigi-debug", action="store_true",
                   help="print fldigi's raw characters on the line above each entry")
    t.add_argument("--min-quality", type=float, metavar="Q",
                   help="only lines with q >= Q (reads morse.jsonl)")
    t.add_argument("--min-coherence", type=float, metavar="C",
                   help="only lines with coherence >= C, e.g. 0.6")
    t.add_argument("--min-chars", type=int, metavar="N",
                   help="only lines with at least N characters (junk is short)")
    t.add_argument("--hide-filtered", action="store_true",
                   help="skip lines the daemon filtered out of the text log")
    t.set_defaults(func=cmd_tail)

    sc = sub.add_parser("scan", help="survey a band with an RTL-SDR (reports only)")
    sc.add_argument("--band", help="preset: 80m 40m 30m 20m 17m 15m 10m")
    sc.add_argument("--start", type=float, metavar="KHZ", help="scan from this kHz")
    sc.add_argument("--stop", type=float, metavar="KHZ", help="scan to this kHz")
    sc.add_argument("--top", type=int, default=6, help="probe the N strongest peaks (6)")
    sc.add_argument("--dwell", type=float, default=8.0, help="seconds per candidate (8)")
    sc.add_argument("--survey-seconds", type=float, default=3.0,
                    help="length of the rtl_power sweep (3)")
    sc.add_argument("--device", help="RTL-SDR serial or index")
    sc.add_argument("--ppm", type=int, default=0, help="frequency correction")
    sc.add_argument("--gain", default="auto", help="tuner gain in dB, or auto")
    sc.add_argument("--hf", action="store_true", help="HF via direct sampling")
    sc.add_argument("--bias-tee", action="store_true", help="power a bias tee")
    sc.add_argument("--mode", default="usb", help="rtl_fm demod (usb, lsb, am)")
    sc.add_argument("--list-bands", action="store_true", help="show the presets")
    sc.set_defaults(func=cmd_scan)

    sd = sub.add_parser("sdr", help="tune a hardware RTL-SDR and feed the cable")
    sd.add_argument("freq", nargs="?", help="frequency: 7.03 (MHz), 7030 (kHz) or 7030000 (Hz)")
    sd.add_argument("--list", action="store_true", help="enumerate the sticks attached")
    sd.add_argument("--stop", action="store_true", help="stop the SDR feed")
    sd.add_argument("--status", action="store_true", help="is the SDR feed running?")
    sd.add_argument("--mode", choices=list(sdr.CW_MODES), default=None,
                    help="demodulation: usb or lsb for CW, am for wide signals")
    sd.add_argument("--device", default=None,
                    help="device index or serial (see --list); needed to pick between two sticks")
    sd.add_argument("--ppm", type=int, default=0,
                    help="frequency correction in ppm (calibrate with rtl_test)")
    sd.add_argument("--gain", default=None,
                    help="tuner gain in dB, or 'auto' (ignored for HF direct sampling)")
    sd.add_argument("--sample-rate", type=int, default=24000,
                    help="audio rate fed to the cable (default 24000)")
    sd.add_argument("--hf", action="store_true",
                    help="HF below 24 MHz via direct sampling (RTL-SDR Blog V3 only)")
    sd.add_argument("--bias-tee", action="store_true",
                    help="power the antenna port (RTL-SDR Blog V3: -T)")
    sd.set_defaults(func=cmd_sdr)

    wp = sub.add_parser("wpm", help="measure the incoming fist's speed (WPM)")
    wp.add_argument("--set", type=float, metavar="WPM", help="set it manually")
    wp.add_argument("--auto", action="store_true", help="measure and save it")
    wp.add_argument("--seconds", type=float, default=6.0)
    wp.add_argument("--tone", type=float, default=None,
                    help="CW tone to measure (default: the decoder's current tone)")
    wp.add_argument("--measure", action="store_true", help=argparse.SUPPRESS)
    wp.set_defaults(func=cmd_wpm)
    rt = sub.add_parser("retime", help="re-space Farnsworth CW (W1AW 5 WPM) to standard CW")
    rt.add_argument("file", help="audio file (any format ffmpeg reads, or s16le 8k .raw)")
    rt.add_argument("-o", "--out", help="output .raw (default: <file>.retimed.raw)")
    rt.add_argument("--tone", type=float, default=None, help="CW tone (default: current)")
    rt.add_argument("--wpm", type=float, default=14.0,
                    help="target character speed (default 14)")
    rt.add_argument("--play", action="store_true", help="play the result into the cable")
    rt.set_defaults(func=cmd_retime)

    tr = sub.add_parser("translate", help="try the CW abbreviation glossary on text")
    tr.add_argument("text", nargs="*", help="text to translate (or pipe it in)")
    tr.add_argument("--mode", choices=["gloss", "replace", "off"])
    tr.add_argument("--all-modes", action="store_true", help="show replace mode and the legend")
    tr.add_argument("--no-marks", action="store_true",
                    help="drop the '?' on context-dependent tokens")
    tr.set_defaults(func=cmd_translate)

    dl = sub.add_parser("dial", help="set the dial frequency (recorded with new lines, live)")
    dl.add_argument("mhz", nargs="?", type=float, help="frequency in MHz, e.g. 3.545")
    dl.add_argument("--clear", action="store_true", help="stop recording a frequency")
    dl.set_defaults(func=cmd_dial)

    tu = sub.add_parser("tune", help="set or auto-detect the decoder CW tone")
    tu.add_argument("--tone", type=float)
    tu.add_argument("--auto", action="store_true")
    tu.add_argument("--seconds", type=float, default=3.5)
    tu.set_defaults(func=cmd_tune)

    se = sub.add_parser("selftest", help="decode a synthetic CW signal end to end")
    se.add_argument("--wpm", type=float, default=20)
    se.add_argument("--noise", type=float, default=0.0)
    se.add_argument("--tone", type=float)
    se.add_argument("--keep", action="store_true", help="leave fldigi running")
    se.set_defaults(func=cmd_selftest)

    co = sub.add_parser("config", help="show or change settings")
    co.add_argument("--set", metavar="KEY=VALUE")
    co.set_defaults(func=cmd_config)

    lg = sub.add_parser("logs", help="list the log files")
    lg.set_defaults(func=cmd_logs)

    pr = sub.add_parser("prune", help="archive or delete logs older than N days")
    pr.add_argument("--days", type=int, metavar="N",
                    help="keep this many days (default: retention_days, 0 = off)")
    pr.add_argument("--dry-run", action="store_true",
                    help="say what would be pruned, change nothing")
    pr.add_argument("--delete", action="store_true",
                    help="delete instead of archiving to <home>/archive")
    pr.add_argument("--force", action="store_true",
                    help="run even while the logger is up (it holds the JSONL open)")
    pr.set_defaults(func=cmd_prune)

    lv = sub.add_parser("level", help="measure the cable level and trim the source volume")
    lv.add_argument("--set", action="store_true", help="apply the correction")
    lv.add_argument("--dry-run", action="store_true",
                    help="say what would change, change nothing")
    lv.add_argument("--target", type=float, metavar="DBFS",
                    help="target peak level (default: level_target_db, -14)")
    lv.add_argument("--seconds", type=float, default=1.0,
                    help="how much audio to measure (1 s)")
    lv.add_argument("--auto", action="store_true",
                    help="let the daemon trim it when it drifts")
    lv.add_argument("--no-auto", action="store_true", help="stop the automatic trim")
    lv.set_defaults(func=cmd_level)
    return p


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # `morse-logger sdr start 7.03` reads naturally, but "start" is the default anyway:
    # drop the verb so the frequency lands in the positional slot.
    if len(argv) >= 2 and argv[0] == "sdr" and argv[1] in ("start", "up"):
        del argv[1]
    # `route` was renamed to `route-audio`: keep the old spelling working, but
    # map it before parsing so --help lists only the real verb.
    if argv and argv[0] == "route":
        print("note: 'route' is now 'route-audio'")
        argv[0] = "route-audio"
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except pulse.PulseError as e:
        print(f"audio error: {e}")
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
