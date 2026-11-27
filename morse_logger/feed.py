"""What the decoder is listening to, right now.

`status` answers "is the process healthy"; this answers a different question
that keeps people staring at an empty log: which audio is arriving. The answer
is one of a stick, one or more programs playing into the cable, a hardware
input or another program's cable looped into it, a chosen capture device - or
nothing at all, which is the case worth naming out loud.

Everything here is gathered from the sound server and /proc, never from the
config alone: config says what was asked for, and the point of the command is
what is actually happening.
"""

import os

from . import pulse

BROWSERS = ("chrome", "chromium", "firefox", "brave", "vivaldi", "opera",
            "edge", "epiphany", "midori")
PLAYERS = ("vlc", "mpv", "mplayer", "rhythmbox", "audacious", "clementine",
           "strawberry", "deadbeef", "spotify", "parole", "celluloid", "totem",
           "amberol", "elisa", "cmus", "smplayer")
# players rtl_fm's audio is piped through; they are the feed itself, not a
# second program, and would otherwise be reported as one
PIPES = ("pacat", "pw-cat", "paplay", "pw-play")


def kind(app, stream=""):
    """browser / player / "" - a hint, never a claim about what is playing."""
    hay = f"{app} {stream}".lower()
    if any(b in hay for b in BROWSERS):
        return "browser"
    if any(p in hay for p in PLAYERS):
        return "player"
    return ""


def label(app, stream=""):
    what = kind(app, stream)
    return f"{app} ({what})" if what else app


# ---------------------------------------------------------------- the SDR feed
def sdr_pidfile(home):
    return os.path.join(home, "run", "sdr.pid")


def sdr_processes():
    """Every morse_logger.sdr_run on the machine, whatever home started it.

    A feed started from another home (a test, a second profile) is invisible to
    this one's pid file - which is how a stray test feed once played into a live
    cable for 40 minutes.
    """
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\x00", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if "morse_logger.sdr_run" in cmd:
            home = ""
            try:
                with open(f"/proc/{entry}/environ", "rb") as f:
                    env = f.read().split(b"\x00")
                for kv in env:
                    if kv.startswith(b"MORSE_LOGGER_HOME="):
                        home = kv.split(b"=", 1)[1].decode("utf-8", "replace")
            except OSError:
                pass
            found.append((int(entry), home))
    return found


def sdr_running(home):
    """True when an SDR feed was started here: pid file alive, or a supervisor.

    Used by `level`, `doctor` and the daemon's level watch to tell "too quiet"
    (a volume to raise) from "an empty band" (nothing to raise).
    """
    try:
        with open(sdr_pidfile(home)) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return True
    except (OSError, TypeError, ValueError):
        pass
    return bool(sdr_processes())


def parse_rtl_args(args):
    """Pick the facts out of an rtl_fm argv: -f frequency, -M mode, -g gain, -p ppm."""
    info: dict = {"freq": None, "mode": None, "gain": None, "ppm": None,
                  "device": None}
    for i, arg in enumerate(args[:-1]):
        if arg == "-f":
            try:
                info["freq"] = int(args[i + 1])
            except ValueError:
                pass
        elif arg == "-M":
            info["mode"] = args[i + 1]
        elif arg == "-g":
            info["gain"] = args[i + 1]
        elif arg == "-p":
            info["ppm"] = args[i + 1]
        elif arg == "-d":
            info["device"] = args[i + 1]
    return info


def rtl_processes():
    """Every running rtl_fm, with its frequency and gain from its command line.

    /proc rather than `pgrep -f`: no self-matching, no dependence on the
    pattern quoting, and the argv is exactly what we want to show.
    """
    out = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        args = [a.decode(errors="replace") for a in raw.split(b"\0") if a]
        if not args or not os.path.basename(args[0]).startswith("rtl_fm"):
            continue
        out.append({"pid": int(pid), "cmd": args, **parse_rtl_args(args)})
    return sorted(out, key=lambda row: row["pid"])


# ---------------------------------------------------------------- the cable
def device_of(cfg):
    """The capture device the decoder opens (the cable unless told otherwise)."""
    return cfg.get("input_device") or pulse.SOURCE


def cable_of(cfg):
    """The sink side of that device: morse-logger_cable_src -> morse-logger_cable."""
    dev = device_of(cfg)
    return dev[:-4] if dev.endswith("_src") else pulse.SINK


def short_device(name):
    """alsa_input.pci-0000_00_1f.3.HiFi__Headset__source -> HiFi__Headset__source"""
    return name.removeprefix("alsa_input.").removeprefix("alsa_output.")


def _state_file(cfg):
    home = cfg.get("_home")
    return os.path.join(home, "run", "pulse-modules.json") if home else None


def playing(cable, cfg=None):
    """Streams whose destination is our cable sink (our own loopbacks excluded)."""
    own = pulse.our_modules(_state_file(cfg or {}))
    return [row for row in pulse.sink_input_details()
            if (row.get("sink_name") or "") == cable
            and not pulse.is_own_loopback(row, own)]


def loops(cable):
    """(module id, source, sink) loopbacks that end on our cable."""
    return [(mid, src, snk) for mid, src, snk in pulse.loopback_modules()
            if (snk or "") == cable]


# ---------------------------------------------------------------- the words
def summary(cfg, streams=(), loop_rows=(), rtl=(), sdr=False):
    """One line: what is feeding the decoder."""
    cable = cable_of(cfg)
    device = cfg.get("input_device")
    if device and not device.endswith("_src"):
        return f"{short_device(device)} (chosen capture device)"
    if sdr or rtl:
        if not rtl:
            return "RTL-SDR configured, but rtl_fm is not running"
        first = rtl[0]
        where = (f"{first['freq'] / 1e6:.3f} MHz"
                 if first["freq"] else "an unreadable frequency")
        mode = (first.get("mode") or "usb").upper()
        return f"RTL-SDR: rtl_fm (pid {first['pid']}) at {where} {mode}"
    if streams:
        if len(streams) == 1:
            return (f"{label(streams[0].get('app', '?'), streams[0].get('stream', ''))}"
                    f" -> {cable}")
        return (f"{len(streams)} programs -> {cable} "
                f"({', '.join(sorted({s.get('app', '?') for s in streams}))})")
    if loop_rows:
        src = loop_rows[0][1] or "?"
        return f"{short_device(src)} looped into {cable}"
    return f"{cable}: nothing feeding it"


def details(cfg, streams=(), loop_rows=(), rtl=()):
    """The lines behind the summary: device, stick, streams, loops, missing ones."""
    device = device_of(cfg)
    out = [f"device   {device}"
           + ("" if not device.endswith("_src") else " (the cable)")]
    for row in rtl:
        extra = []
        if row.get("gain"):
            extra.append(f"gain {row['gain']}")
        if row.get("ppm"):
            extra.append(f"ppm {row['ppm']}")
        out.append(f"rtl      pid {row['pid']}  {' '.join(row['cmd'])}"
                   + (f"  ({', '.join(extra)})" if extra else ""))
    for row in streams:
        app = row.get("app", "?")
        media = row.get("stream") or ""
        hint = kind(app, media)
        out.append(f"stream   {row.get('index')}  {app}"
                   + (f" ({hint})" if hint else "")
                   + (f'  "{media}"' if media and media != "Playback Stream" else "")
                   + f"  -> {row.get('sink_name')}")
    for mid, src, snk in loop_rows:
        out.append(f"loop     module {mid}  {src} -> {snk}")
    for name in cfg.get("source_loops") or []:
        if not any(src == name for _mid, src, _snk in loop_rows):
            why = ("no such capture source" if not pulse.source_exists(name)
                   else "source exists but nothing loops it into the cable")
            out.append(f"missing  {name} (configured, {why})")
    return out


def describe(cfg):
    """Gather from the system and return (summary, detail lines)."""
    home = cfg.get("_home") or os.path.expanduser("~/morse-logger")
    sdr = sdr_running(home)
    rtl = rtl_processes() if sdr else []
    cable = cable_of(cfg)
    streams = playing(cable, cfg)
    if sdr:
        # rtl_fm is piped into the cable by pacat/pw-cat; it is the feed itself,
        # described by the rtl lines, not a second program
        streams = [row for row in streams if (row.get("app") or "") not in PIPES]
    loop_rows = loops(cable)
    if (not sdr and not streams and not loop_rows
            and device_of(cfg).endswith("_src") and not pulse.sink_exists(cable)):
        # "nothing feeding it" would be a lie: the cable itself is gone
        return (f"{cable}: no such sink - create it with 'morse-logger cable up'",
                details(cfg, streams, loop_rows, rtl))
    return (summary(cfg, streams, loop_rows, rtl, sdr=sdr),
            details(cfg, streams, loop_rows, rtl))