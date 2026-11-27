# Resolved issues

Kept out of FIXME.txt so the open work stays short. Each item carries the
commit that closed it, so the reasoning stays findable.

- Log format: make it identical to js8call-logger - equally spaced fields, coh=
  shown, unknown values as --- rather than omitted.
  DONE bba188b. The CW line now uses the same widths as js8call-logger (dial 10,
  q/snr 8, coh 7, repeat/channel 4, tone 6, callsign 9), so the decoded text
  starts at column 75 in both logs and `cut -c75-` reads either. CW has no
  sending station, so the callsign slot is `?` (same placeholder JS8 uses when
  unknown), and the repeat marker sits in the 4-wide channel slot.

- stop_source_loops() skips any loopback whose source contains ".monitor", so a
  loop that pulls another program's cable monitor into our cable can never be
  removed with `route-audio --stop-source`.
  DONE bba188b. The tap is now matched exactly (default sink's monitor -> our
  cable) and compared as a whole argument; a line-in or another logger's
  `<name>_cable.monitor` is a removable source loop. The test that documented
  this as an expected failure is now an ordinary passing test.

- `route-audio --copy` creates the tap but records nothing: cfg["tap"] is read
  by ensure_cable and written nowhere. `cable up --no-monitor` only changed the
  live state, unlike `start --no-monitor`.
  DONE 178ec72. --copy/--stop-copy, --source/--stop-source and cable up/down now
  persist tap/source_loops/monitor in config.json, cable down clears them, and
  the daemon re-creates saved loops on start. cable up --no-monitor now persists
  exactly like start --no-monitor.

- CPU scaling is written back, so it compounds (300 -> 1200 -> 4800 on a slow
  machine). Keep the scale in an underscore field and apply it at read time.
  DONE c018720. load_config() sets _cpu_scale and leaves intervals as stored;
  logger.scaled() applies the profile where an interval is read. Checked the
  real ~/morse-logger/config.json: 600/600/60, i.e. no drift on this machine.

- is_own_loopback() identifies our loopbacks by media.name starting with
  "loopback-", so another program's loopbacks are treated as ours.
  DONE bba188b. Sink-input rows now carry Owner Module; our loopbacks are the
  ones whose module id is recorded in run/pulse-modules.json, and the prefix is
  only a fallback when no state file exists. Another tool's loop is visible to
  route-audio again.

- Retention: `prune --days N [--dry-run] [--delete]`, retention_days 0, archive
  by default, refuse while the daemon holds the JSONL.
  DONE 700714c. morse_logger/retention.py does exactly that for the daily text
  files, morse.jsonl (rewritten atomically, unparsable lines kept, old lines
  gzipped) and the SQLite rows (deleted + VACUUM), all to the same cutoff.
  `morse-logger prune` refuses while the daemon holds the lock (--force
  overrides).

- Routing flapping: route_new_streams() re-asserts every poll with no memory.
  DONE 2ee6196. Moves are counted per app over a minute; after three the daemon
  warns once, names the sharing fix (route-audio --source
  <other>_cable.monitor) and backs off for five minutes.

- Persist what route-audio did so a restart rebuilds it, and let
  `route-audio --source` accept another program's `<name>_cable.monitor`.
  DONE 178ec72. monitor_sources() offers other programs' outputs in
  --list-sources and loop_source() accepts them; the saved list is restored by
  the daemon on start and by the 30 s cable watchdog.

- Repair events where a human will see them.
  DONE 6863d69. Runner.repair() appends a `#` line to the daily text log, keeps
  the last one in status, and best-effort notify-sends (deduplicated for ten
  minutes). Wired to watchdog retunes, silence autotune, speed restarts,
  re-created cable, restored loops and giving up after five failed restarts.

- Trim the level, do not just report it.
  DONE 5cccf1a. `morse-logger level` reports peak dBFS/volume/verdict and works
  out a correction capped at 6 dB; --set applies it, digital silence is never
  amplified, volume never passes 100%. `level --auto` persists level_auto and
  the daemon re-checks every 20 s (scaled for slow CPUs).

- Group conversations: "CW has no groups, but the same split maps to net vs
  station if nets are ever tracked."
  NOT APPLICABLE. CW decoding yields no sender/destination or net identity, so
  there is nothing to split into conversations/ or groups/ yet; the note stays
  as a design idea for if net tracking is ever added.

- `cable up` ignored the saved `monitor` choice, then overwrote it: with no
  flag it built the loopback anyway and persisted `True`, so a bare `cable up`
  from a helper script or restore step silently re-enabled monitoring and
  rebuilt the cable->speakers loopback (a doubled copy ~100 ms apart, i.e. an
  echo).
  DONE da2fc08. cli._want_monitor() is tri-state as in js8call-logger:
  `--monitor` and `--no-monitor` win, neither means `cfg["monitor"]`, and
  config is written only when the answer differs from what is stored. A bare
  `cable up` with monitoring saved off now passes monitor=False to
  ensure_cable, so no loopback is created. Both flags exist on the `cable`
  subcommand (previously only --no-monitor), and README documents them.

- No doctor check for the doubled-audio condition itself.
  DONE da2fc08. pulse.monitor_loops_to_default() returns the module ids of
  loopbacks whose source is one of our cables' `.monitor` and whose sink is the
  default sink; it ignores feeds into the cable and other programs' monitors.
  doctor now prints `[ok] no monitor loopback into your speakers`, `[ok] one
  monitor loopback ... (monitoring on)`, or a `[!!]` naming the modules when
  there are two, or when one exists although monitoring is saved off. Proven
  on the dev machine (`[ok] one monitor loopback into your speakers`); the
  fault path and B12/B13 are in docs/HARDWARE-TESTS.md.

- Ported back from js8call-logger: at 100% volume `level` said "raise the
  volume in the source application", which is wrong when an RTL-SDR feed is
  driving the cable - there is no application, only rtl_fm's tuner gain.
  DONE a730d25. pulse.trim_volume() takes `at_full_hint`, defaulting to the
  old wording; cli._at_full_hint() reports the rtl_fm feed (tracked pid, or any
  morse_logger.sdr_run on the machine) and cmd_level passes "the cable is fed
  by rtl_fm, not an application - raise the tuner gain: 'morse-logger sdr start
  <MHz> --gain <dB>'". Tests: test_pulse.py at-100% with and without the hint,
  test_cli.py for both feeds.

- Ported back from js8call-logger: its selftest helper `_last_text` picked the
  *oldest* record in its 25-line window when no --expect was given, so a live
  check printed yesterday's decode as its evidence.
  NOT APPLICABLE. That helper was fixed to walk the window newest-first, but
  morse-logger's selftest reads fldigi's text output from a byte offset rather
  than the log file, so it has no equivalent bug.

- Ported back from js8call-logger: `level --auto` on an RTL-SDR feed trimmed the
  capture volume toward -14 dBFS on an empty band, where a low peak is nothing
  in the passband rather than a volume to raise (measured there: gain 0 -> 49.6
  moved the reading only from -40.5 to -28.1 dBFS).
  DONE e195e5f. pulse.level_verdict(db, sdr=False) is the single word for a
  level ("too quiet" / "no signal in the passband" / "clipping" / "ok");
  feed.sdr_running(home) answers "is a stick feeding this cable" (pid file, or
  any morse_logger.sdr_run). cmd_level, doctor and daemon.check_level all use
  the pair: level stops before trim_volume with "that is an rtl_fm feed's noise
  floor, not a volume problem", doctor prints the empty band as `[--]` rather
  than `[!!]`, and --auto returns early, so the capture volume is left alone
  while the band is empty. Clipping protection and the digital-silence refusal
  are unchanged. doctor's quiet/clipping bounds moved to level's documented
  -20/-10 dBFS (they were -30/-1.5, contradicting its own "aim for -20 to -10"
  advice). Tests: test_pulse.py LevelVerdictTest, test_cli.py for both feeds,
  test_daemon.py for the skip and for a quiet application still being trimmed.
  The live half (a real dongle, an empty band, then a fixture) is D10-D14 in
  docs/HARDWARE-TESTS.md; verified here without a stick: `input` named a
  pw-play stream playing a file into a real cable, `level` at 100% gave the
  application-volume advice, and after `cable down` it said `no such sink`.

- Ported back from js8call-logger: say what the decoder is listening to.
  DONE e195e5f. morse_logger/feed.py gathers from the sound server and /proc,
  never from config: pulse.loopback_modules() (copied) turns `pactl list
  modules` into (id, source, sink), sink_input_details() already gave
  {index, app, stream, sink_name}, and rtl_processes() reads each rtl_fm argv
  for -f/-M/-g/-p. Answers: `Google Chrome (browser) -> morse-logger_cable` with
  the tab title in the detail line, `<device> looped into ...` for a hardware
  input, `RTL-SDR: rtl_fm (pid N) at 7.030 MHz USB`, `RTL-SDR configured, but
  rtl_fm is not running`, `missing <name> (configured, ...)` for a saved loop
  that is gone, `no such sink - create it with 'morse-logger cable up'`, and
  `nothing feeding it`. Our own monitor loopback is excluded by module id
  (pulse.is_own_loopback), and rtl_fm's pacat/pw-cat pipe is not reported as a
  second program. `morse-logger input` prints summary + detail lines; `status`
  adds the summary as a `feed` line and never fails on it. Read-only: the device
  chooser stays in `config`. Tests: tests/test_feed.py (22 cases, no pactl
  needed). Documented in README's command table and a new "What is feeding the
  decoder" section; tools/audit_verbs.py is clean (21 verbs).
