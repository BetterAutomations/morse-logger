"""The logging daemon: supervise fldigi, tail its decoded text, journal it."""
import json
import os
import signal
import sys
import time
import traceback
from datetime import datetime, timezone

from collections import deque

from . import autotune, fldigi, logger, pulse, translate, wpm


class Runner:
    def __init__(self, cfg, verbose=True, allow_tty=True):
        self.cfg = cfg
        self.verbose = verbose
        self.home = cfg["_home"]
        self.run_dir = os.path.join(self.home, "run")
        os.makedirs(self.run_dir, exist_ok=True)
        self.status_path = os.path.join(self.run_dir, "status.json")
        self.stop_requested = False
        self.eng = None
        self.tailer = None
        self.asm = logger.Assembler(cfg["line_gap_s"], cfg["max_line_chars"],
                                    cfg.get("line_flush_s", 10.0),
                                    cfg.get("min_emit_chars", 8),
                                    cfg.get("merge_timeout_s", 20.0),
                                    cfg.get("line_target_chars", 88),
                                    cfg.get("line_idle_flush_s", 25.0),
                                    cfg.get("break_on_bt", True))
        mode = cfg.get("translate", "gloss")
        self.journals = logger.Journals(
            cfg["log_dir"], legend=translate.legend() if mode != "off" else None,
            max_line=int(cfg.get("log_line_max", 150)))
        # the scans are the only real CPU cost we control, so let the profile
        # decide their workspace
        autotune.KEYED_STEP = int(cfg.get("autotune_step_hz", 25))
        autotune.REFINE_STEP = int(cfg.get("autotune_refine_hz", 10))
        self.lines = 0
        self.reload_requested = False
        self.measured = None          # (wpm, when) from the last successful probe
        self.failed_starts = 0
        if cfg.get("_log_dir_moved"):
            was, now = cfg["_log_dir_moved"]
            print(f"note: config.json pointed the logs at {was}; using {now} "
                  f"(logs live with the data directory)", flush=True)
        # glossed text is longer than the raw decode; this ratio is learned from
        # each record so the line target can aim at the *rendered* width
        self._target_max = int(cfg.get("line_target_chars", 60))
        self._target_warned = False
        self.last = None
        self.last_text = None
        self.last_time = 0.0
        self.repeat_id = None
        self.repeat_count = 0
        self.started = time.time()
        self.restarts = 0
        self._autotuned = False
        self.peak_quality = None
        self.recent = deque(maxlen=max(2, int(cfg.get("watchdog_window", 6))))
        self.retunes = 0
        self.last_retune = 0.0
        self.last_speed_check = 0.0
        self.last_silence_check = 0.0
        self.speed_restarts = 0

    # ---------- helpers ----------
    def say(self, msg):
        if self.verbose:
            stamp = datetime.now().strftime("%H:%M:%S")
            print(f"[{stamp}] {msg}", flush=True)

    def write_status(self, extra=None):
        eng = self.eng
        st = {
            "pid": os.getpid(),
            "started_utc": datetime.fromtimestamp(self.started, timezone.utc).isoformat(),
            "uptime_s": int(time.time() - self.started),
            "lines": self.lines,
            "last_line": self.last,
            "fldigi": {
                "pid": eng.proc.pid if (eng and eng.proc) else None,
                "running": bool(eng and eng.healthy()),
                "tone_hz": self._safe(lambda: eng.rpc.carrier()),
                "modem": self._safe(lambda: eng.rpc.modem_name()),
                "quality": self._safe(lambda: eng.rpc.quality()),
            },
            "input_device": self.cfg["input_device"],
            "dial_mhz": logger.read_dial(self.cfg.get("dial_mhz"), self.home),
            "restarts": self.restarts,
            "last_quality": getattr(self, "last_q", None),
            "last_coherence": getattr(self, "last_coh", None),
            "watchdog_retunes": getattr(self, "retunes", 0),
            "speed_restarts": getattr(self, "speed_restarts", 0),
            "phase": getattr(self, "phase", "logging"),
            "wpm": self.cfg.get("wpm"),
            "wpm_measured": self.measured[0] if self.measured else None,
            "wpm_measured_ago": int(time.time() - self.measured[1]) if self.measured else None,
            "cable": pulse.cable_status(),
            "log_dir": self.cfg["log_dir"],
            "textout": eng.textout if eng else None,
        }
        if extra:
            st.update(extra)
        tmp = self.status_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(st, f, indent=2)
        os.replace(tmp, self.status_path)
        return st

    @staticmethod
    def _safe(fn):
        try:
            return fn()
        except Exception:
            return None

    # ---------- lifecycle ----------
    def start_engine(self):
        cfg = self.cfg
        # one source of truth at run time: the control file, seeded from config
        logger.write_dial(cfg.get("dial_mhz"), self.home)
        pulse.ensure_cable(state_file=os.path.join(self.run_dir, "pulse-modules.json"),
                           monitor=bool(cfg.get("monitor")),
                           tap=bool(cfg.get("tap")))
        self.eng = fldigi.Fldigi(
            home=self.home,
            input_device=cfg["input_device"],
            tone=cfg["tone_hz"],
            wpm=cfg["wpm"],
            farnsworth_wpm=cfg.get("farnsworth_wpm", 0),
            bandwidth=cfg["bandwidth_hz"],
            matched_filter=cfg["matched_filter"],
            port=cfg["xmlrpc_port"],
            binary=cfg.get("fldigi_binary"),
            log=self.say,
        )
        self.eng.start()
        # fldigi decodes whatever clears its threshold; with the squelch off it
        # happily turns the noise floor into letters. Apply our settings now.
        try:
            self.eng.rpc.call("main.set_squelch_level", float(cfg.get("squelch_level", 30)))
            self.eng.rpc.call("main.set_squelch", 1 if cfg.get("squelch", True) else 0)
        except Exception as exc:
            self.say(f"could not set the decoder squelch: {exc}")
        self.tailer = logger.Tailer(self.eng.textout, from_end=True)  # skip stale text
        self.asm.cancel()
        self._autotuned = False
        self.peak_quality = None

    def autotune(self, seconds=4.0):
        if not self.eng:
            return None
        data, best = b"", None
        for window in (seconds, 6.0, 8.0):
            data += pulse.capture_samples(window, rate=8000, source=self.cfg["input_device"])
            if autotune.level(data) < 20:
                self.say("autotune: the cable is silent - nothing is playing into it")
                return None
            best = autotune.choose(data)
            if best and best.get("tone_hz"):
                break
        if not best or not best.get("tone_hz"):
            self.say("autotune: no CW tone found (no steady carrier, no keyed tone)")
            return None
        if best.get("clipping"):
            self.say("autotune: input is clipped at 0 dBFS - lower the volume, or you "
                     "have a feedback loop (a 'loopback-' stream must not sit on the "
                     "cable; see 'morse-logger route-audio --list')")
            return best
        if not best["usable"]:
            self.say(f"autotune: no steady CW tone found (best {best['tone_hz']} Hz, "
                     f"steady {int(best['stability'] * 100)}%) - leaving decoder at "
                     f"{self.cfg['tone_hz']} Hz. Tune a signal in and run: morse-logger tune --auto")
            return best
        self.eng.rpc.set_carrier(best["tone_hz"])
        hz, q, _tried = fldigi.refine_carrier(self.eng.rpc, best["tone_hz"], log=self.say)
        self.cfg["tone_hz"] = hz
        self.say(f"autotune: CW tone {hz} Hz "
                 f"(steady in {int(best['stability'] * 100)}% of windows, quality {q})")
        return best

    def restart_engine(self, keep_tone=True):
        """Stop and start fldigi. Used when a setting can only be applied at
        startup - the CW decoder has no run-time speed control."""
        try:
            if self.eng:
                self.eng.stop()
        except Exception as exc:                      # noqa: BLE001
            self.say(f"restart: stop failed ({exc})")
        self.eng = None
        self.tailer = None
        self.start_engine()
        if keep_tone:
            self._autotuned = True                    # tone is already right

    def probe_wpm(self, label=None):
        """Measure the fist on the cable. Returns the measurement or None.

        Shared by the startup measurement and the periodic watchdog so both
        apply exactly the same guards.
        """
        cfg = self.cfg
        # The listen budget must fit inside the watchdog interval: probing blocks
        # the loop, so a short interval cannot afford a 30 s listen.
        windows = wpm.listen_windows(float(cfg.get("speed_watchdog_interval_s", 240)))
        m = wpm.measure_live(
            lambda sec: pulse.capture_samples(sec, rate=8000, source=cfg["input_device"]),
            tone_hz=cfg.get("tone_hz"), windows=tuple(windows),
            say=(self.say if label else None))
        if not m:
            if label:
                self.say(f"{label}: no keyed CW to measure - leaving the speed alone")
            return None
        if "wpm" in m:
            self.measured = (round(float(m["wpm"]), 1), time.time())
        if "rejected" in m:
            if label:
                self.say(f"{label}: {m['rejected']} - leaving the speed alone")
            return None
        if m.get("confidence", 0) < float(cfg.get("speed_watchdog_min_confidence", 0.5)):
            if label:
                self.say(f"{label}: keying found but confidence only "
                         f"{m['confidence']:.2f} - leaving the speed alone")
            return None
        return m

    def adopt_wpm(self, m, label, restart):
        """Use a measurement if it differs enough to matter.

        `restart` is True for the periodic watchdog (fldigi is already running and
        only reads the speed at startup) and False at startup, where the engine
        has not been created yet and will pick the value up anyway.
        """
        cfg = self.cfg
        cur = float(self.cfg.get("wpm") or 0)
        if not 6 <= m["wpm"] <= 45:
            # Outside hand-sent CW: adopting it would wreck the decode for the
            # rest of the session, and a wrong speed is worse than a stale one.
            self.say(f"{label}: measured {m['wpm']:g} WPM is outside the "
                     f"plausible 6-45 WPM range - not adopting it")
            return False
        new = int(round(m["wpm"]))
        change = (abs(m["wpm"] - cur) / cur) if cur else 1.0
        if cur and (change < float(cfg.get("speed_watchdog_change", 0.2))
                    or abs(m["wpm"] - cur) < 2):
            self.say(f"{label}: fist measures {m['wpm']:g} WPM, decoder is at {cur:g} "
                     f"- no change")
            return False
        if new == int(cur):
            return False
        cfg["wpm"] = new
        logger.save_config(cfg)
        self.say(f"{label}: fist measures {m['wpm']:g} WPM (dot {m['dot_ms']:g} ms, "
                 f"confidence {m['confidence']:.2f}); decoder was {cur:g} WPM -> {new} WPM")
        if restart:
            self.speed_restarts += 1
            self.restart_engine(keep_tone=True)
            self.say(f"{label}: decoder restarted at {new} WPM "
                     f"(restart #{self.speed_restarts})")
        return True

    def measure_wpm_on_start(self):
        """Set the decoder speed from the audio before fldigi is even launched."""
        if not self.cfg.get("wpm_auto_on_start", True):
            return
        pulse.ensure_cable(state_file=os.path.join(self.run_dir, "pulse-modules.json"),
                           monitor=bool(self.cfg.get("monitor")),
                           tap=bool(self.cfg.get("tap")))
        m = self.probe_wpm("wpm at startup")
        if m:
            self.adopt_wpm(m, "wpm at startup", restart=False)

    def check_speed(self, now):
        """Periodic speed watchdog: restart the decoder if the fist drifted."""
        cfg = self.cfg
        if not cfg.get("speed_watchdog", True):
            return
        if now - self.last_speed_check < float(cfg.get("speed_watchdog_interval_s", 90)):
            return
        self.last_speed_check = now
        if self.speed_restarts >= int(cfg.get("speed_watchdog_max_restarts", 4)):
            return
        # never cut a line in half: wait for a gap in the transmission
        gap = float(cfg.get("line_gap_s", 2.5)) + 3.0
        if self.last_time and now - self.last_time < gap:
            return
        m = self.probe_wpm("speed watchdog")
        if m:
            self.adopt_wpm(m, "speed watchdog", restart=True)

    def _route_watch(self):
        """Keep the app's audio on the cable across browser reloads.

        PipeWire forgets a moved stream when the stream ends, so reloading the
        WebSDR tab silently drops the audio back to the speakers and the logger
        goes deaf. Re-route anything from the applications `morse-logger route-audio` saw.
        """
        cfg = self.cfg
        if not cfg.get("route_watch", True):
            return
        apps = cfg.get("route_apps") or []
        if not apps:
            return
        try:
            moved, self._routed = pulse.route_new_streams(
                apps=apps, known=getattr(self, "_routed", set()))
        except Exception:                     # noqa: BLE001 - never kill the loop
            return
        for idx, app in moved:
            self.say(f"route: {app} started playing (stream {idx}) - moved it to the cable")

    def check_silence(self, now):
        """Signal present but nothing decoded for a long time = probably detuned.

        The retune watchdog in `_maybe_retune` needs transmissions to reason
        about; if the tone is far enough off, nothing decodes at all and it can
        never fire. This covers that case by looking at the audio itself.
        """
        cfg = self.cfg
        if not cfg.get("watchdog", True):
            return
        quiet_s = float(cfg.get("watchdog_silence_s", 300))
        if now - self.last_silence_check < quiet_s:
            return
        if self.last_time and now - self.last_time < quiet_s:
            return
        self.last_silence_check = now
        m = self.probe_wpm()                      # is anything keying at all?
        if not m:
            return
        self.say(f"watchdog: keying is present ({m['wpm']:g} WPM) but nothing has "
                 f"decoded in {quiet_s / 60:.0f} min - decoder is probably "
                 f"off-frequency ({cfg.get('tone_hz')} Hz), re-running autotune")
        self.autotune()

    def _maybe_retune(self, coh_pct, quality, now):
        """Strong tone + consistently unreadable text is the detuning signature.

        A signal only ~45 Hz off the decoder carrier produces perfect-looking
        tone quality and gibberish text, so we require both: several bad
        transmissions in a row AND good quality on each of them.
        """
        if not self.cfg.get("watchdog", True) or quality is None:
            return
        self.recent.append((coh_pct, quality))
        if len(self.recent) < self.recent.maxlen:
            return
        cool = float(self.cfg.get("watchdog_cooldown_s", 180))
        if now - self.last_retune < cool:
            return
        # "at or below", so a line that scores exactly the threshold counts as bad
        coh_thr = float(self.cfg.get("watchdog_coherence", 45))
        q_thr = float(self.cfg.get("watchdog_min_quality", 40))
        if not all(c <= coh_thr for c, _q in self.recent):
            return
        if not all(q >= q_thr for _c, q in self.recent):
            return
        self.last_retune = now
        self.retunes += 1
        self.recent.clear()
        self.say(f"watchdog: {self.recent.maxlen} unreadable transmissions in a row "
                 f"with good tone quality - the decoder is probably off-frequency "
                 f"({self.cfg.get('tone_hz')} Hz), re-running autotune")
        best = self.autotune()
        if best and best.get("usable"):
            self.say(f"watchdog: decoder moved to {self.cfg.get('tone_hz')} Hz "
                     f"(retune #{self.retunes})")
        else:
            self.say("watchdog: autotune found no usable tone; leaving the carrier alone")

    def run(self):
        self._lock = logger.acquire_lock(self.home)
        if self._lock is None:
            holder = logger.lock_holder(self.home) or "unknown"
            print(f"another morse-logger daemon is already running for this data "
                  f"directory (pid {holder}) - exiting", flush=True)
            return 3
        signal.signal(signal.SIGTERM, self._sig)
        signal.signal(signal.SIGINT, self._sig)
        # SIGUSR1 asks a *running* logger to re-read config.json and apply it;
        # this is how `morse-logger wpm` changes speed without a manual restart
        signal.signal(signal.SIGUSR1, self._sig_reload)
        self.set_phase("measuring the fist")
        self.measure_wpm_on_start()          # before fldigi starts: no restart
        self.set_phase("starting the decoder")
        self.start_engine()
        self.set_phase("tuning the decoder")
        if self.cfg.get("autotune_on_start"):
            self.autotune()
        self.set_phase("logging")
        self.say(f"logging to {self.cfg['log_dir']}")
        last_status = 0.0
        while not self.stop_requested:
            try:
                now = time.time()
                if getattr(self, "reload_requested", False):
                    self.reload_requested = False
                    self.apply_reload()
                if not self.eng.healthy():
                    self.say("fldigi is not responding - restarting")
                    self._restart()
                    continue
                chunk = self.tailer.read()
                if chunk:
                    for line in self.asm.feed(chunk, now):
                        self.emit(line, now)
                if self.asm.buf:
                    # quality is only non-zero while a signal is present, so we
                    # keep the best value seen during this transmission
                    q = self._safe(lambda: self.eng.rpc.quality())
                    if q is not None and (self.peak_quality is None or q > self.peak_quality):
                        self.peak_quality = q
                for line in self.asm.maybe_flush(now):
                    self.emit(line, now)
                self.check_speed(now)
                self.check_silence(now)
                if now - getattr(self, "last_route_check", 0) >= 3:
                    self.last_route_check = now
                    self._route_watch()
                if now - last_status > 2:
                    self.write_status()
                    last_status = now
                time.sleep(float(self.cfg.get("poll_interval_s", 0.2)))
            except Exception:
                self.say("error in main loop:\n" + traceback.format_exc())
                self._restart()
        self.shutdown()

    def _sig_reload(self, signum, frame):
        self.reload_requested = True

    def apply_reload(self):
        """Re-read config and apply it to the live decoder."""
        old_wpm, old_tone = self.cfg.get("wpm"), self.cfg.get("tone_hz")
        fresh = logger.load_config(self.home)
        self.cfg.update({k: v for k, v in fresh.items() if k != "_home"})
        new_wpm, new_tone = self.cfg.get("wpm"), self.cfg.get("tone_hz")
        if new_tone != old_tone:
            self._safe(lambda: self.eng.rpc.set_carrier(int(new_tone)))
            self.say(f"reload: tone {old_tone} -> {new_tone} Hz")
        if new_wpm != old_wpm:
            self.say(f"reload: speed {old_wpm} -> {new_wpm} WPM "
                     f"(fldigi reads speed only at launch, restarting it)")
            self.restart_engine()
        else:
            self.say("reload: settings applied")

    def set_phase(self, phase):
        """Record what startup is doing so `morse-logger start` can say something useful."""
        self.phase = phase
        try:
            self.write_status()
        except Exception:                     # noqa: BLE001
            pass

    def _restart(self):
        self.restarts += 1
        if self.eng is not None and not self.eng.display_alive():
            self.say("the X display died - starting a fresh one before the decoder")
        try:
            if self.eng:
                self.eng.stop()
        except Exception:
            pass
        # The tracked process can already be dead while a child it spawned still
        # holds the XML-RPC port - that is what produced 77 restarts in a row,
        # each one failing with "port already in use" while nothing was decoded.
        # Kill anything using our config dir before trying again.
        try:
            cfgdir = self.eng.config_dir if self.eng else os.path.join(self.home, "fldigi")
            fldigi.reap_strays(cfgdir, log=self.say)
        except Exception:                      # noqa: BLE001
            pass
        backoff = min(15, 2 * self.restarts)
        self.say(f"restarting decoder in {backoff}s (restart #{self.restarts})")
        time.sleep(backoff)
        try:
            self.start_engine()
            self.failed_starts = 0
        except Exception as e:
            self.eng = None                    # never leave a half-built engine behind
            self.failed_starts = getattr(self, "failed_starts", 0) + 1
            self.say(f"restart failed: {e}")
            if self.failed_starts >= 5:
                self.say("giving up after 5 failed restarts without a working decoder "
                         "- fix the cause above, then run: morse-logger restart")
                self.stop_requested = True

    def _sig(self, signum, frame):
        self.say(f"signal {signum}: shutting down")
        self.stop_requested = True

    def shutdown(self):
        for line in self.asm.flush_all():
            self.emit(line, time.time())
        if self.eng:
            try:
                self.eng.stop()
            except Exception:
                pass
        self.write_status({"state": "stopped"})
        self.journals.close()
        try:
            os.unlink(os.path.join(self.run_dir, "morse-logger.pid"))
        except OSError:
            pass
        self.say("stopped")

    # ---------- logging ----------
    def emit(self, line, now):
        text = line["text"]
        if len(text) < self.cfg["min_chars"]:
            self.peak_quality = None
            return
        carrier = self._safe(lambda: self.eng.rpc.carrier())
        quality = self.peak_quality
        if quality is None:
            quality = self._safe(lambda: self.eng.rpc.quality())
        quality = round(quality, 1) if quality is not None else None
        self.peak_quality = None
        # keep the raw decode for the JSONL, but mark unresolved characters in
        # everything a human reads (glossing happens on the marked text, so the
        # English prose is untouched)
        marker = self.cfg.get("uncertain_char", "~")
        shown_text = logger.mark_uncertain(text, marker)
        mode = self.cfg.get("translate", "gloss")
        coh_pct = int(round(translate.coherence(text) * 100))
        gloss_ok = coh_pct >= float(self.cfg.get("gloss_min_coherence", 45))
        marks = bool(self.cfg.get("translate_uncertain", True))
        if gloss_ok:
            units, _n = translate.translate_units(shown_text, mode, mark_uncertain=marks)
        else:
            units, _n = shown_text.split(" "), 0

        # One record per physical line. If the glossed text will not fit, split it
        # at a token boundary and log the remainder as its own record with the
        # same timestamp - never as an indented fragment, which reads like a new
        # transmission that lost its metadata.
        avail = max(40, (self.journals.max_line or 150) - 63)
        toks = text.split(" ")
        parts = [(toks, units)]
        if len(" ".join(units)) > avail and len(toks) == len(units) and len(units) > 1:
            chunks, cur, used = [], [], 0
            for tok, unit in zip(toks, units):
                width = len(unit) + (1 if cur else 0)
                if cur and used + width > avail:
                    chunks.append(cur)
                    cur, used = [(tok, unit)], len(unit)
                else:
                    cur.append((tok, unit))
                    used += width
            if cur:
                chunks.append(cur)
            if len(chunks) > 1:
                parts = [([t for t, _u in c], [u for _t, u in c]) for c in chunks]

        for i, (ptoks, punits) in enumerate(parts):
            sub = dict(line)
            sub["text"] = " ".join(ptoks)
            if i:
                sub["continued"] = 1      # same transmission, next line
            self._emit_part(sub, punits, quality, coh_pct, carrier, mode,
                            gloss_ok, now)

    def _emit_part(self, line, units, quality, coh_pct, carrier, mode, gloss_ok, now):
        text = line["text"]
        shown = " ".join(units)
        rec = {
            "text": text,                    # raw decode, always kept
            "text_xlated": shown if (mode != "off" and gloss_ok) else None,
            "quality": quality,
            "coherence": coh_pct,
            "carrier_hz": carrier,
            # read live so `morse-logger dial 3.545` takes effect without a restart
            "dial_mhz": logger.read_dial(self.cfg.get("dial_mhz"), self.home),
            "started_utc": datetime.fromtimestamp(line["started"] or now,
                                                  timezone.utc).isoformat(),
            "repeat_count": 1,
        }
        for flag in ("partial", "continuing", "continued"):
            if line.get(flag):
                rec[flag] = 1
        if line.get("split"):
            rec["split"] = line["split"]
        rec["_units"] = units                     # atomic chunks for wrapping
        # filters affect the human-readable log only; JSONL/sqlite keep everything
        min_q = float(self.cfg.get("min_quality") or 0)
        min_c = float(self.cfg.get("min_coherence") or 0)
        rec["filtered"] = 1 if ((quality is not None and quality < min_q) or coh_pct < min_c) else 0
        if text == self.last_text and (now - self.last_time) < self.cfg["repeat_window_s"]:
            self.repeat_count += 1
            rec["repeat_count"] = self.repeat_count
            rec["repeat_of"] = self.repeat_id
        else:
            self.repeat_count = 1
            rec["repeat_of"] = None
        pretty, stored = self.journals.write(
            rec, datetime.fromtimestamp(line["started"] or now).astimezone())
        # Self-correcting target: shorten only when a record actually overflowed,
        # then creep back up. A gloss ratio would overshoot - it swings per record.
        if self.cfg.get("line_target_auto", True):
            if len(pretty) > (self.journals.max_line or 150):
                if not self._target_warned:
                    print("note: shortening the line target - the glossed text "
                          "is overflowing", flush=True)
                    self._target_warned = True
                self.asm.target_chars = max(35, self.asm.target_chars - 6)
            elif self.asm.target_chars < self._target_max:
                self.asm.target_chars += 1
        self.repeat_id = stored.get("id")
        self.last_text, self.last_time, self.last = text, now, pretty
        self.last_q, self.last_coh = quality, coh_pct
        self._maybe_retune(coh_pct, quality, now)
        self.lines += 1
        print(pretty + ("   [filtered]" if rec.get("filtered") else ""), flush=True)
