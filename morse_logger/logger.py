"""Log assembly: tail fldigi's rx text stream and turn it into timestamped logs.

fldigi appends decoded characters (and occasional newlines at arbitrary points)
to talk/textout.txt.  We therefore rebuild *lines* the way an operator reads
them: a line ends when the decoder has been silent for `line_gap_s` seconds.
"""
import json
import os
import sqlite3
import time
from datetime import datetime, timezone

# prefix width of one log line: date-time (19) + 2 + bracket (51) + 2.
# The decoded text always starts at this offset, in both this program and
# js8call-logger, so `cut -c75-` reads the same column from either log.
HEADER_WIDTH = 74

DEFAULT_CONFIG = {
    "input_device": "morse-logger_cable_src",
    "output_device": "default",
    "tone_hz": 1500,
    "wpm": 20,
    "farnsworth_wpm": 0,
    "bandwidth_hz": 150,
    "matched_filter": True,
    "xmlrpc_port": 7362,
    "dial_mhz": None,
    "line_gap_s": 2.0,
    "line_flush_s": 10.0,
    "route_apps": [],
    "route_watch": True,
    # what route-audio did, so a restart rebuilds it instead of leaving the
    # cable up without the copy loop or the line input
    "tap": False,
    "source_loops": [],
    "min_emit_chars": 12,
    "merge_timeout_s": 10.0,
    "break_on_bt": True,
    # fldigi emits lowercase for characters it only partly resolved; Morse has no
    # case, so some of these are real letters. Show them as a marker, not as text.
    "uncertain_char": "~",
    "line_target_chars": 60,
    "line_target_auto": True,
    "log_line_max": 150,
    "line_idle_flush_s": 25.0,
    "max_line_chars": 160,
    "min_chars": 2,
    "repeat_window_s": 120,
    "notify": True,          # desktop notification for repair events
    # 0 keeps everything; `prune --days N` or this setting turns it on
    "retention_days": 0,
    "log_dir": None,
    "monitor": True,
    "tap": False,
    "translate": "gloss",
    "translate_uncertain": True,
    "gloss_min_coherence": 45,
    "min_quality": 0,
    "min_coherence": 0,
    "autotune_on_start": True,
    "watchdog": True,
    "watchdog_window": 8,
    "autotune_step_hz": 25,          # band workspace for the keying search
    "poll_interval_s": 0.2,          # how often the supervisor loop wakes
    "watchdog_coherence": 45,
    "watchdog_min_quality": 40,
    "squelch": True,             # fldigi gates decoding on its squelch: off = letters from noise
    "squelch_level": 30,         # 0-100; raise to reject weaker QRM, lower to hear weak signals
    "watchdog_cooldown_s": 600,
    "speed_watchdog": True,
    "wpm_auto_on_start": True,
    "watchdog_silence_s": 600,
    "speed_watchdog_interval_s": 60,
    "speed_watchdog_change": 0.2,
    "speed_watchdog_max_restarts": 4,
    "speed_watchdog_min_confidence": 0.5,
    "fldigi_binary": None,
}


def home_dir():
    """Where config, logs and runtime state live.

    MORSE_LOGGER_HOME overrides it; nothing else does. (CWLOG_HOME used to be
    honoured as an alias, but a stale copy in a shell silently redirected logs to
    the pre-rename directory - the exact bug that made "status" report a path the
    daemon was not using.)
    """
    value = os.environ.get("MORSE_LOGGER_HOME")
    if value:
        return os.path.expanduser(value)
    return os.path.expanduser("~/morse-logger")


def load_config(home=None, create=True):
    home = home or home_dir()
    path = os.path.join(home, "config.json")
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(path):
        try:
            with open(path) as f:
                cfg.update(json.load(f))
        except Exception as e:
            raise RuntimeError(f"bad config {path}: {e}")
    cfg["_home"] = home
    # create everything on demand: the installer is not the only entry point.
    # `create=False` is for read-only verbs, so asking for "status" can never
    # resurrect a directory the user just deleted.
    if create:
        for sub in ("", "logs", "run"):
            os.makedirs(os.path.join(home, sub) if sub else home, exist_ok=True)
    if create and not os.path.exists(path):
        try:
            with open(path, "w") as f:
                json.dump({k: v for k, v in cfg.items() if not k.startswith("_")},
                          f, indent=2)
                f.write("\n")
        except OSError:
            pass          # a read-only home is still usable, just not saved
    # Always measured, never guessed from a model name. Fast is the default:
    # the profile only moves when the benchmark says this machine is slow.
    from . import cpu as _cpu
    score, profile = _cpu.detect(home)
    cfg["_cpu_score"] = score
    cfg["_cpu_profile"] = profile
    cfg["_cpu_scale"] = {"fast": 1.0, "medium": 2.0, "slow": 4.0}[profile]
    if profile == "slow":
        # cheaper scans and a sleepier loop: the two things that actually cost
        # time on a 1.2 GHz dual core, beyond the intervals below. Both are
        # idempotent, unlike a multiplied interval.
        cfg["autotune_step_hz"] = max(50, int(cfg.get("autotune_step_hz", 25)))
        cfg["autotune_refine_hz"] = max(20, int(cfg.get("autotune_refine_hz", 10)))
        cfg["poll_interval_s"] = max(0.4, float(cfg.get("poll_interval_s", 0.2)))
        # a restart costs far more here than a slightly stale speed guess
        cfg["speed_watchdog_change"] = max(0.25, float(cfg.get("speed_watchdog_change", 0.2)))
    # Intervals stay exactly as stored; the profile is applied by scaled() when
    # one is read. Writing the stretched number back made the scale compound on
    # every save (300 -> 1200 -> 4800 on a slow machine).
    # Every score shares one 0-100 scale, but an early config could hold the
    # fraction form (0.45). Comparing 0.45 against coh=19 silently disabled the
    # threshold, which glossed noise as if it were copy.
    for key in ("gloss_min_coherence", "min_coherence", "min_quality",
                "watchdog_coherence"):
        val = cfg.get(key)
        if isinstance(val, (int, float)) and 0 < val <= 1:
            cfg[key] = round(float(val) * 100, 1)
    want = os.path.abspath(os.path.join(home, "logs"))
    got = os.path.abspath(os.path.expanduser(cfg.get("log_dir") or want))
    if got != want and not got.startswith(os.path.abspath(home) + os.sep):
        # config.json carried an absolute path from before MORSE_LOGGER_HOME was
        # set (the migration only moved files). Keeping logs with the data is the
        # only reading that cannot surprise anyone.
        cfg["_log_dir_moved"] = (got, want)
        got = want
    cfg["log_dir"] = got
    return cfg


def scaled(cfg, key, default):
    """An interval from the config, stretched for a slow CPU.

    The stored value is the user's; the profile multiplies it here, so saving
    the config can never fold the scale into it.
    """
    try:
        val = float(cfg.get(key, default))
    except (TypeError, ValueError):
        val = float(default)
    try:
        scale = float(cfg.get("_cpu_scale") or 1.0)
    except (TypeError, ValueError):
        scale = 1.0
    return val * scale


def dial_path(home=None):
    return os.path.join(home or home_dir(), "run", "dial")


def write_dial(mhz, home=None):
    """Publish the dial frequency the running daemon should stamp on new lines."""
    p = dial_path(home)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        f.write("" if mhz is None else f"{float(mhz):g}")
    os.replace(tmp, p)
    return p


def read_dial(default=None, home=None):
    """Current dial frequency in MHz, or `default` when unset."""
    try:
        with open(dial_path(home)) as f:
            raw = f.read().strip()
    except OSError:
        return default
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return default



def mark_uncertain(text, marker="~"):
    """Replace fldigi's lowercase (unresolved) characters with a marker.

    `?` cannot be used - CW really sends it (..--..) - so the default is `~`,
    which is not a Morse character and does not collide with the log's other
    punctuation ([glosses], ? for R, ... for a partial line).
    """
    if not marker or not text:
        return text
    import re
    return re.sub(r"[a-z]", marker, text)


def lock_path(home=None):
    return os.path.join(home or home_dir(), "run", "morse-logger.lock")


def acquire_lock(home=None):
    """Exclusive lock for the daemon. Returns an open handle, or None if taken.

    A pid file is not enough: a second daemon overwrites it, and the first one
    becomes untracked while still holding the XML-RPC port. Both then declare
    the other's fldigi dead and restart forever, and nothing gets logged.
    """
    import fcntl
    path = lock_path(home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # "a" (not "w"): opening for write would truncate the pid of the daemon that
    # already holds the lock, so a second start would erase the very information
    # it reports ("another daemon is running, pid ...").
    handle = open(path, "a")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def lock_holder(home=None):
    """pid of the daemon holding the lock, or None."""
    try:
        raw = open(lock_path(home)).read().strip()
    except OSError:
        return None
    try:
        pid = int(raw)
    except ValueError:
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return pid

def save_config(cfg, home=None):
    home = home or home_dir()
    os.makedirs(home, exist_ok=True)
    clean = {k: v for k, v in cfg.items() if not k.startswith("_")}
    with open(os.path.join(home, "config.json"), "w") as f:
        json.dump(clean, f, indent=2)
        f.write("\n")
    return os.path.join(home, "config.json")


class Tailer:
    """Follow a file that may be truncated or replaced (fldigi restarts)."""

    def __init__(self, path, from_end=False):
        self.path = path
        self.pos = 0
        self.ino = None
        self.opened = False
        self.from_end = from_end

    def _reopen(self):
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            self.pos, self.ino, self.opened = 0, None, False
            return False
        self.ino = st.st_ino
        self.pos = st.st_size if self.from_end else 0
        self.opened = True
        return True

    def read(self):
        if not self.opened:
            if not self._reopen():
                return ""
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            self.opened = False
            return ""
        if st.st_ino != self.ino or st.st_size < self.pos:
            # rotated or truncated by a fresh fldigi instance: a different file
            # (or a shorter one) always means read it from the start. Keeping the
            # old offset for a *larger* replacement skips the first bytes of the
            # new file - exactly the text a restart decodes first.
            self.ino = st.st_ino
            self.pos = 0
        if st.st_size == self.pos:
            return ""
        try:
            with open(self.path, "rb") as f:
                f.seek(self.pos)
                data = f.read()
                self.pos = f.tell()
        except OSError:
            return ""
        return data.decode("utf-8", errors="replace")


class Assembler:
    """Accumulate decoded characters and emit readable lines.

    A line ends when the decoder has been silent for `gap_s` (the operator
    stopped sending). That is the natural unit - but a ragchew over can run for
    a minute or more, and waiting for the pause means nothing appears in the log
    while it happens. So a chunk is also emitted once it is `flush_s` old, cut
    at a word boundary and marked as continuing, so the copy appears live
    without cutting words in half.
    """

    def __init__(self, gap_s=2.0, max_chars=160, flush_s=10.0,
                 min_emit_chars=12, merge_timeout_s=10.0,
                 target_chars=88, idle_flush_s=25.0, break_on_bt=True):
        self.gap_s = gap_s
        self.max_chars = max_chars
        self.flush_s = flush_s
        self.min_emit_chars = min_emit_chars
        self.merge_timeout_s = merge_timeout_s
        self.break_on_bt = break_on_bt            # a <BT> closes the line
        self.target_chars = target_chars          # text chars per line (target)
        self.idle_flush_s = idle_flush_s          # flush a short remnant after this
        self.buf = []
        self.started = None
        self.last_char = None
        self._continuing = False      # previous chunk was cut mid-transmission
        # A burst shorter than min_emit_chars is held here and joined to the
        # next one, instead of becoming a 3-character line of its own.
        self.carry = ""
        self.carry_started = None
        self.carry_last = None

    def feed(self, text, now=None):
        now = now or time.time()
        out = []
        for ch in text:
            if ch in "\r\n":
                # fldigi inserts newlines at arbitrary buffer points: treat as
                # whitespace, the silence gap is what really ends a line
                if self.buf and self.buf[-1] != " ":
                    self.buf.append(" ")
                continue
            if ch == "\x00":
                continue
            if self.started is None:
                if self.carry:                 # continue the held-back burst
                    self.buf = list(self.carry + " ")
                    self.started = self.carry_started
                    self.carry = ""
                else:
                    self.started = now
            self.buf.append(ch)
            self.last_char = now
            if self.break_on_bt and "".join(self.buf[-4:]) == "<BT>":
                # <BT> ends the paragraph: close the line here, keep what follows
                # for the next one. A <BT> with almost nothing before it is more
                # likely a misread than a paragraph break, so it stays inline.
                body = " ".join("".join(self.buf[:-4]).split())
                if len(body) >= max(4, self.min_emit_chars // 2):
                    rec = self._take(split="BT")
                    if rec:
                        out.append(rec)
                continue
            if len(self.buf) >= self.max_chars:
                out.append(self._take())
        return [t for t in out if t]

    def maybe_flush(self, now=None):
        now = now or time.time()
        if self.carry and self.carry_last is not None \
                and (now - self.carry_last) >= self.merge_timeout_s:
            rec = {"text": self.carry, "started": self.carry_started}
            self.carry = ""
            self.carry_last = None
            return [rec]
        if not self.buf:
            return []
        # Emit on *size*, not on the gap: a transmission boundary is a poor place
        # to break a line if the result is two dozen characters. Accumulate until
        # the line is the requested length, and only flush a short remnant once
        # the band has been quiet long enough that nothing more is coming.
        text = " ".join("".join(self.buf).split())
        if self.target_chars and len(text) >= self.target_chars:
            rec = self._take(now)
            return [rec] if rec else []
        idle = now - (self.last_char or now)
        if idle >= self.idle_flush_s:
            rec = self._take(now)
            return [rec] if rec else []
        return []

    def flush_all(self, now=None):
        """Emit anything still held, including a short carry. Used at shutdown."""
        out = []
        if self.buf:
            rec = self._take(now or time.time())
            out += [rec] if rec else []
        if self.carry:
            out.append({"text": self.carry, "started": self.carry_started})
            self.carry = ""
            self.carry_last = None
        return out

    def _hold_or_emit(self, rec, now):
        """Hold a too-short burst back so the next one can be joined to it."""
        if not rec:
            return None
        if self.min_emit_chars and len(rec["text"]) < self.min_emit_chars:
            self.carry = rec["text"]
            self.carry_started = rec["started"]
            self.carry_last = now
            return None
        return rec

    def _record(self, raw, started, partial, split=None):
        text = " ".join(raw.split())
        if not text:
            return None
        rec = {"text": text, "started": started}
        if split:
            rec["split"] = split
        if partial:
            rec["partial"] = True
        if self._continuing:
            rec["continuing"] = True
        return rec

    def _take(self, now=None, split=None):
        raw = "".join(self.buf)
        started, self.started = self.started, None
        continuing = self._continuing          # must be read before it is cleared
        self.buf = []
        self._continuing = False
        rec = self._record(raw, started, partial=False, split=split)
        if rec is not None and continuing:
            rec["continuing"] = True
        return self._hold_or_emit(rec, now)

    def _take_at_word(self, now):
        """Cut at the last space so a word is never split mid-way."""
        raw = "".join(self.buf)
        cut = raw.rstrip().rfind(" ")
        if cut <= 0:
            return None               # single long word: wait for the gap
        head, rest = raw[:cut + 1], raw[cut + 1:]
        rec = self._record(head, self.started, partial=True)
        if rec is None:
            return None
        self.buf = list(rest)
        self.started = now            # the remainder starts now, not at the over start
        self._continuing = True
        return rec

    def cancel(self):
        self.buf, self.started, self.last_char = [], None, None
        self._continuing = False


class Journals:
    """Daily text log + JSONL + sqlite, all append-only and crash-safe."""

    def __init__(self, log_dir, legend=None, max_line=150):
        self.dir = log_dir
        self.legend = legend
        self.max_line = max_line            # hard limit for one physical line
        os.makedirs(log_dir, exist_ok=True)
        self.db_path = os.path.join(log_dir, "morse.sqlite3")
        self._db = None
        self._day = None
        self._txt = None
        self._jsonl = None

    def _roll(self, dt):
        day = dt.strftime("%Y-%m-%d")
        if day == self._day:
            return
        for f in (self._txt, self._jsonl):
            if f:
                f.close()
        self._day = day
        path = os.path.join(self.dir, f"morse-{day}.txt")
        fresh = not os.path.exists(path) or os.path.getsize(path) == 0
        self._txt = open(path, "a", buffering=1)
        if fresh and self.legend:
            self._txt.write(f"# {self.legend}\n")
        self._jsonl = open(os.path.join(self.dir, "morse.jsonl"), "a", buffering=1)

    def _conn(self):
        if self._db is None:
            self._db = sqlite3.connect(self.db_path, timeout=10)
            self._db.execute("""CREATE TABLE IF NOT EXISTS captures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_utc TEXT, ended_utc TEXT, text TEXT, chars INTEGER,
                quality REAL, carrier_hz INTEGER, dial_mhz REAL,
                repeat_of INTEGER, repeat_count INTEGER)""")
            # migrate older logs in place: raw text stays in `text`
            cols = [r[1] for r in self._db.execute("PRAGMA table_info(captures)")]
            if "text_xlated" not in cols:
                self._db.execute("ALTER TABLE captures ADD COLUMN text_xlated TEXT")
            if "coherence" not in cols:
                self._db.execute("ALTER TABLE captures ADD COLUMN coherence REAL")
            if "filtered" not in cols:
                self._db.execute("ALTER TABLE captures ADD COLUMN filtered INTEGER")
            if "split" not in cols:
                self._db.execute("ALTER TABLE captures ADD COLUMN split TEXT")
            self._db.commit()
        return self._db

    def _wrap(self, units, prefix, indent="  \u2026 "):
        """Physical lines for one record: prefix + units, each <= max_line.

        Units are the renderer's atomic chunks, so an expansion such as
        "all stations[CQ]" moves to the next line whole rather than being split
        from the token it explains.
        """
        if not self.max_line or not units:
            return [prefix + " ".join(units)]
        lines, cur, used = [], [], 0
        avail = self.max_line - len(prefix)
        for u in units:
            add = len(u) + (1 if cur else 0)
            if cur and used + add > avail:
                lines.append(" ".join(cur))
                cur, used = [u], len(u)
                avail = self.max_line - len(indent)
            else:
                cur.append(u)
                used += add
        if cur:
            lines.append(" ".join(cur))
        return [prefix + lines[0]] + [indent + l for l in lines[1:]]

    def write(self, rec, when=None):
        """`when` is when the transmission started (used for the stamp)."""
        when = when or datetime.now().astimezone()
        now = datetime.now().astimezone()
        self._roll(when)
        stamp = when.strftime("%Y-%m-%d %H:%M:%S")
        # metadata goes first, in fixed-width fields, so the decoded text always
        # starts in the same column (63, col 64) whatever is known or missing.
        # The widths match js8call-logger's line (dial 10, q/snr 8, coh 7,
        # repeat/channel 4, tone 6, callsign 9), so `cut -c64-` works on both.
        # CW does not know the sending station, so the callsign slot is `?`, the
        # same placeholder that program uses when a callsign is unknown.
        dial = f"{rec['dial_mhz']:7.3f}MHz" if rec.get("dial_mhz") else "    ---MHz"
        quality = rec.get("quality")
        qual = f"q={quality:6.1f}" if quality is not None else "q=     -"
        coh = f"coh={rec['coherence']:3d}" if isinstance(rec.get("coherence"), int) else "coh=  -"
        tone = f"{rec['carrier_hz']:4d}Hz" if rec.get("carrier_hz") else "   -Hz"
        n_rep = rec.get("repeat_count", 1)
        rep = ("999+" if n_rep > 999 else f"x{n_rep}") if n_rep > 1 else ""
        who = (rec.get("station") or "?")[:9]
        head = f"[{dial} {qual} {coh} {rep:>4s} {tone} {who:<9}]  "
        units = rec.get("_units")
        if units is None:                        # raw text, or nothing glossed
            units = (rec.get("text_xlated") or rec["text"]).split(" ")
        if rec.get("continuing"):
            units = ["\u2026"] + units          # picks up a chunk cut mid-over
        if rec.get("partial"):
            units = units + ["\u2026"]          # this over is still going
        prefix = f"{stamp}  {head}"
        physical = self._wrap(units, prefix)
        line = physical[0]
        if not rec.get("filtered"):
            self._txt.write("".join(l + "\n" for l in physical))
        rec_out = {k: v for k, v in rec.items() if not k.startswith("_")}
        rec_out["logged_at"] = now.isoformat()
        self._jsonl.write(json.dumps(rec_out, ensure_ascii=False) + "\n")
        db = self._conn()
        cur = db.execute(
            "INSERT INTO captures (started_utc, ended_utc, text, chars, quality,"
            " carrier_hz, dial_mhz, repeat_of, repeat_count, text_xlated,"
            " coherence, filtered, split)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rec.get("started_utc"), now.astimezone(timezone.utc).isoformat(),
             rec["text"], len(rec["text"]), rec.get("quality"),
             rec.get("carrier_hz"), rec.get("dial_mhz"),
             rec.get("repeat_of"), rec.get("repeat_count", 1),
             rec.get("text_xlated"), rec.get("coherence"),
             rec.get("filtered", 0), rec.get("split")))
        db.commit()
        rec_out["id"] = cur.lastrowid
        return line, rec_out

    def note(self, text, when=None):
        """A system event in the daily text log, marked with `#`.

        The marker can never match the traffic format, so a repair is visible
        where a human is already reading without looking like decoded copy.
        """
        when = when or datetime.now().astimezone()
        self._roll(when)
        line = f"{when.strftime('%Y-%m-%d %H:%M:%S')}  # {text}"
        self._txt.write(line + "\n")
        return line

    def close(self):
        for f in (self._txt, self._jsonl):
            if f:
                f.close()
        self._txt = self._jsonl = None
        if self._db is not None:
            self._db.close()
            self._db = None
