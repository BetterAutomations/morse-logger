r"""Virtual audio cable management via pactl/parec (pipewire-pulse or pulseaudio).

The "cable" is a null sink plus a remapped source, so any program can play into
the sink (the cable's input) and we can capture from the source (the cable's
output) without touching the system default devices.

    program --play--> [ morse-logger_cable sink ] --monitor--> [ morse-logger_cable_src source ] --> decoder
                                   \--> loopback --> speakers (optional, so you
                                        can still hear the WebSDR)
"""
import json
import os
import re
import shutil
import subprocess
import time

SINK = "morse-logger_cable"
SOURCE = "morse-logger_cable_src"
SINK_DESC = "Morse logger virtual cable"
SOURCE_DESC = "Morse logger cable source"


def sink_name(channel=1):
    """Channel 1 keeps the original name; later channels get a suffix."""
    return SINK if channel <= 1 else "%s_%d" % (SINK, channel)


def source_name(channel=1):
    return SOURCE if channel <= 1 else "%s_src_%d" % (SINK, channel)


def cable_channels():
    """Which cable channels exist right now, e.g. [1, 2]."""
    names = {r[1] for r in sinks() if len(r) > 1}
    return [ch for ch in range(1, 9) if sink_name(ch) in names]


class PulseError(RuntimeError):
    pass


def _run(args, timeout=10, check=True, input_bytes=None):
    exe = shutil.which(args[0])
    if not exe:
        raise PulseError(f"'{args[0]}' not found. Install pulseaudio-utils / pipewire-pulse.")
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout, input=input_bytes)
    except subprocess.TimeoutExpired:
        raise PulseError(f"timeout running: {' '.join(args)}")
    if check and p.returncode != 0:
        raise PulseError(f"{' '.join(args)}: {p.stderr.decode(errors='replace').strip()}")
    return p


def available():
    return bool(shutil.which("pactl")) and bool(shutil.which("parec"))


def server_info():
    p = _run(["pactl", "info"], check=False)
    info = {}
    for line in p.stdout.decode(errors="replace").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            info[k.strip()] = v.strip()
    return info


def _rows(kind):
    """kind: sinks|sources|sink-inputs|modules"""
    p = _run(["pactl", "list", "short", kind], check=False)
    rows = []
    for line in p.stdout.decode(errors="replace").splitlines():
        parts = line.split("\t")
        rows.append(parts)
    return rows


def sinks():
    return _rows("sinks")


def sources():
    return _rows("sources")


def sink_inputs():
    return _rows("sink-inputs")


def modules():
    return _rows("modules")


def sink_exists(name=SINK):
    return any(len(r) > 1 and r[1] == name for r in sinks())


def source_exists(name=SOURCE):
    return any(len(r) > 1 and r[1] == name for r in sources())


def default_sink_name():
    try:
        p = _run(["pactl", "get-default-sink"], check=False)
        return p.stdout.decode().strip() or None
    except PulseError:
        return None


def _load_module(arg, state_file=None):
    p = _run(["pactl", "load-module"] + arg)
    mid = p.stdout.decode().strip()
    if state_file:
        data = {}
        if os.path.exists(state_file):
            try:
                data = json.load(open(state_file))
            except Exception:
                data = {}
        data.setdefault("modules", []).append(mid)
        os.makedirs(os.path.dirname(state_file), exist_ok=True)
        with open(state_file, "w") as f:
            json.dump(data, f, indent=2)
    return mid


def ensure_cable(state_file=None, monitor=False, rate=48000, tap=False, channel=1):
    """Create the virtual cable if missing. Returns dict of what was done.

    monitor=True : you hear what the cable carries (loopback cable -> speakers)
    tap=True     : the cable carries a copy of everything your speakers play
                   (loopback speakers -> cable), so nothing has to be rerouted
    channel=N    : which cable; 1 is morse-logger_cable, 2 is morse-logger_cable_2
    """
    sink, source = sink_name(channel), source_name(channel)
    done = {"sink": False, "source": False, "monitor": False, "tap": False,
            "monitor_skipped": False, "channel": channel}
    # A tap (speakers -> cable) and monitoring (cable -> speakers) are the same
    # loop wired twice: together they run away to 0 dBFS and the decoder hears
    # nothing but clipping. The tap wins; the caller is told monitoring was left
    # off so it can explain why.
    if monitor and (tap or _tap_active()):
        monitor = False
        done["monitor_skipped"] = True
    if not sink_exists(sink):
        _load_module([
            "module-null-sink",
            f"sink_name={sink}",
            f'sink_properties=device.description="{SINK_DESC}"',
            f"rate={rate}",
        ], state_file)
        done["sink"] = True
        # give pipewire a moment to publish the node
        for _ in range(20):
            if sink_exists(sink):
                break
            time.sleep(0.1)
    if not source_exists(source):
        try:
            _load_module([
                "module-remap-source",
                f"master={sink}.monitor",
                f"source_name={source}",
                f'source_properties=device.description="{SOURCE_DESC}"',
            ], state_file)
            done["source"] = True
        except PulseError:
            # remap may be unavailable; the .monitor source still works
            done["source"] = False
    if tap:
        if not _tap_active():
            dst = default_sink_name()
            if dst and dst != sink:
                _load_module([
                    "module-loopback",
                    f"source={dst}.monitor",
                    f"sink={sink}",
                    "latency_msec=40",
                    "source_dont_move=true",
                    "sink_dont_move=true",
                ], state_file)
                done["tap"] = True
    if monitor:
        if not _monitor_active():
            dst = default_sink_name()
            if dst and dst != sink:
                _load_module([
                    "module-loopback",
                    f"source={sink}.monitor",
                    f"sink={dst}",
                    "latency_msec=60",
                ], state_file)
                done["monitor"] = True
    return done


def _module_blocks():
    """[(module_id, block_text), ...] from `pactl list modules`."""
    p = _run(["pactl", "list", "modules"], check=False)
    text = p.stdout.decode(errors="replace")
    out = []
    for block in text.split("\nModule #")[1:]:
        mid = block.split("\n", 1)[0].strip()
        out.append((mid, block))
    return out


def _monitor_active():
    """True if a loopback from our cable monitor already exists."""
    for _mid, block in _module_blocks():
        if "module-loopback" in block and f"{SINK}.monitor" in block:
            return True
    return False


def _is_tap_block(block):
    if "module-loopback" not in block:
        return False
    if f"{SINK}.monitor" in block:          # cable -> speakers (monitor)
        return False
    # speakers -> cable (tap): sink is our cable
    return f"sink={SINK}" in block or f"sink_name={SINK}" in block


def _tap_active():
    return any(_is_tap_block(b) for _m, b in _module_blocks())


def stop_tap(state_file=None):
    killed = [mid for mid, b in _module_blocks() if _is_tap_block(b)]
    for mid in killed:
        _run(["pactl", "unload-module", mid], check=False)
    _forget_modules(killed, state_file)
    return killed


def stop_monitor(state_file=None):
    """Unload loopback modules that carry our cable monitor to a speaker."""
    killed = []
    for mid, block in _module_blocks():
        if "module-loopback" in block and f"{SINK}.monitor" in block:
            _run(["pactl", "unload-module", mid], check=False)
            killed.append(mid)
    _forget_modules(killed, state_file)
    return killed


def modules_for_channel(channel):
    """Module ids that belong to one cable channel (its sink, source, loops)."""
    sink, source = sink_name(channel), source_name(channel)
    names = [sink, sink + ".monitor", source]
    out = []
    for mid, block in _module_blocks():
        for name in names:
            if re.search(r"(?:sink_name|source_name|master|sink|source)=%s(?:\s|$)"
                         % re.escape(name), block):
                out.append(mid)
                break
    return out


def cable_down(state_file=None, all_loopbacks=True, channel=None):
    """Remove one cable channel, or all of them when channel is None."""
    removed = []
    if all_loopbacks:
        removed += stop_monitor(state_file)
        removed += stop_tap(state_file)
    targets = [channel] if channel else (cable_channels() or [1])
    for ch in targets:
        for mid in modules_for_channel(ch):
            _run(["pactl", "unload-module", mid], check=False)
            removed.append(mid)
    _forget_modules(removed, state_file)
    if state_file and os.path.exists(state_file):
        try:
            os.unlink(state_file)
        except OSError:
            pass
    return removed


def list_modules_for_cable():
    out = []
    for mid, block in _module_blocks():
        if "module-null-sink" in block and f"sink_name={SINK}" in block.replace("Sink Name: ", "sink_name="):
            out.append(mid)
        elif "module-remap-source" in block and f"{SOURCE}" in block:
            out.append(mid)
    return out


def _forget_modules(ids, state_file):
    if not state_file or not os.path.exists(state_file):
        return
    try:
        data = json.load(open(state_file))
        data["modules"] = [m for m in data.get("modules", []) if m not in ids]
        json.dump(data, open(state_file, "w"), indent=2)
    except Exception:
        pass



def source_names():
    """Capture devices worth routing: hardware inputs, not monitors or our remap.

    `--list` shows playback *streams* (applications producing audio); this is the
    other direction - what the machine can record from.
    """
    p = _run(["pactl", "list", "short", "sources"], check=False)
    out = []
    for line in p.stdout.decode(errors="replace").splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        name = parts[1].strip()
        # skip monitors and anything that is one of our own cables (the remap
        # sources are named after the sink, so matching "cable" covers old names)
        if (not name or name.endswith(".monitor") or name == SOURCE
                or name == SINK + ".monitor" or "cable" in name.lower()):
            continue
        out.append(name)
    return out


def loop_source(name, state_file=None, latency_ms=40):
    """Loop a hardware input into the cable. Nothing is moved; the input stays usable."""
    if name not in source_names():
        raise PulseError(f"no such capture source: {name}\n"
                         "see 'morse-logger route-audio --list-sources'")
    for _mid, block in _module_blocks():
        if (f"source={name}" in block and f"sink={SINK}" in block
                and "module-loopback" in block):
            return False                     # already looped
    _load_module([
        "module-loopback",
        f"source={name}",
        f"sink={SINK}",
        f"latency_msec={latency_ms}",
        "source_dont_move=true",
        "sink_dont_move=true",
    ], state_file)
    return True


def stop_source_loops(state_file=None):
    """Remove loopbacks that pull a hardware input into the cable (not the tap)."""
    killed = []
    for mid, block in _module_blocks():
        if "module-loopback" not in block or f"sink={SINK}" not in block:
            continue
        m = re.search(r"(?:^|\s)source=([^\s]+)", block)
        if not m or ".monitor" in m.group(1):
            continue                          # that is the speakers tap, not a line in
        _run(["pactl", "unload-module", mid], check=False)
        killed.append(mid)
    _forget_modules(killed, state_file)
    return killed

def sink_input_details():
    """[{index, sink, app, stream}] for every playing application stream."""
    p = _run(["pactl", "list", "sink-inputs"], check=False)
    text = p.stdout.decode(errors="replace")
    sink_names = {r[0]: r[1] for r in sinks() if len(r) > 1}
    out = []
    for block in text.split("Sink Input #")[1:]:
        idx = block.split("\n", 1)[0].strip()
        info = {"index": int(idx) if idx.isdigit() else idx, "sink": "?",
                "sink_name": "?", "app": "?", "stream": ""}
        for line in block.splitlines():
            line = line.strip()
            if line.startswith("Sink:"):
                info["sink"] = line.split(":", 1)[1].strip()
                info["sink_name"] = sink_names.get(info["sink"], info["sink"])
            elif line.startswith("application.name ="):
                info["app"] = line.split("=", 1)[1].strip().strip('"')
            elif line.startswith("media.name ="):
                info["stream"] = line.split("=", 1)[1].strip().strip('"')
        out.append(info)
    return out


def is_own_loopback(row):
    """True for the sink-inputs that OUR loopback modules create.

    Moving one of these into the cable closes a loop (cable -> loopback ->
    cable), which runs away to 0 dBFS and silences the decoder. Never route
    them.
    """
    return (row.get("stream") or "").startswith("loopback-")


def route_sink_inputs(target=SINK, indices=None):
    """Move running audio streams (e.g. the browser tab) onto the cable.

    Returns (moved, skipped) where skipped lists our own loopback streams and
    anything already playing into the cable.
    """
    moved, skipped = [], []
    for row in sink_input_details():
        idx = str(row["index"])
        label = row["app"] if row["app"] != "?" else row["stream"]
        if indices and idx not in [str(i) for i in indices]:
            continue
        if is_own_loopback(row):
            skipped.append((idx, label, "morse-logger's own monitor loopback"))
            continue
        if row.get("sink_name") == target:
            skipped.append((idx, label, "already on the cable"))
            continue
        p = _run(["pactl", "move-sink-input", idx, target], check=False)
        if p.returncode == 0:
            moved.append((idx, label))
    return moved, skipped


def route_new_streams(target=SINK, apps=(), known=()):
    """Move streams from the given applications that are not on the cable yet.

    Routing is not sticky in PipeWire/PulseAudio: a moved stream keeps its
    destination only for its lifetime, so reloading the browser tab (or
    restarting the app) puts the new stream back on the default sink. The logger
    calls this periodically with the applications `morse-logger route-audio` recorded, so a
    reload is picked up without the operator doing anything.

    `known` is the set of stream indexes already handled, returned updated.
    """
    moved = []
    apps = {a for a in apps if a}
    for row in sink_input_details():
        idx = str(row["index"])
        if row.get("sink_name") == target or is_own_loopback(row):
            continue
        app = row["app"] if row["app"] != "?" else row["stream"]
        if app not in apps:
            continue
        p = _run(["pactl", "move-sink-input", idx, target], check=False)
        if p.returncode == 0:
            moved.append((idx, app))
            known.add(idx)
    return moved, known


def capture_samples(seconds=4.0, rate=8000, source=SOURCE, timeout=None):
    """Capture raw s16le mono PCM from the cable. Returns bytes."""
    if not source_exists(source) and not source.endswith(".monitor"):
        source = f"{SINK}.monitor"
    timeout = timeout or (seconds + 10)
    args = ["parec", f"--device={source}", f"--rate={rate}", "--channels=1",
            "--format=s16le", "--raw", f"--latency-msec=20"]
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        data = p.stdout.read(int(rate * 2 * seconds))
    finally:
        p.terminate()
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.kill()
    return data or b""


def cable_status():
    return {
        "sink": sink_exists(), "source": source_exists(),
        "monitor": _monitor_active(), "sinks": [r[1] for r in sinks() if len(r) > 1],
        "sources": [r[1] for r in sources() if len(r) > 1],
    }
