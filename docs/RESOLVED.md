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
