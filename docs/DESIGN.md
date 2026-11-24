# Design notes and measurements
Internal detail kept out of the README: how the pieces fit, why they are
shaped that way, what was measured on the development machine, and how the
offline bundle is built. Nothing here is needed to use the program.

## Speed watchdog verification

Verified end to end with a real fldigi: the same 12 WPM signal decoded
`T W H A T ARE YOUR PLANS FOR THE WEEK` at the 20 WPM setting, and
`WHAT ARE YOUR PLANS FOR THE WEEK` after the watchdog restarted at 12 WPM.

Timing measurements behind the 60 s default: one probe costs ~0.05 s of CPU but
blocks the loop for its listen window (5 s, extensible to 31 s), so the interval
must exceed the window. The code therefore derives the listen budget from the
interval (interval − 2 s) instead of hoping the two never collide.

## How it fits together

    WebSDR / SDR app / receiver
              |  audio
              v
      [morse-logger_cable ]  pipewire null sink  <-- morse-logger cable up
              |  monitor
              v
      [morse-logger_cable_src]  remapped source
              |
        fldigi (Xvfb, CW modem, XML-RPC on 7362)
              |  talk/textout.txt  (decoded characters, streamed)
              v
      supervisor loop  ->  lines by length  ->  logs/morse-*.txt + .jsonl + .sqlite3

## W1AW recordings and Farnsworth spacing

W1AW's "5 WPM" practice is not 5 WPM elements: it sends ~14 WPM characters with
1.5 s between letters (Farnsworth). Measured from a real recording: dots 84 ms,
dashes 243 ms, element gaps 77 ms, **letter gaps 1501 ms**, word gaps 3505 ms.

fldigi's CW decoder cannot follow that - it loses timing state across the long
gaps. Measured on the same text: normal spacing 86% correct, 0.6 s gaps 80%,
1.5 s gaps 26%. No tone, level, speed or `CWFARNSWORTH` setting fixes it.

`morse-logger retime` fixes it by re-emitting the audio at standard 1/3/7 dot spacing at
the same character speed:

```
morse-logger retime test_recordings/260107_05WPM.mp3 --tone 750 --wpm 14 --play
```

It reports what it measured (`marks 1032 (580 dots, 452 dashes), 281 letter
gaps, 92 word gaps; 965s in -> 310s out`) and writes `<file>.retimed.raw`. The
retimed audio decodes as ordinary CW:

```
OW 5 WPM BT TEXT IS FROM MARCH 2024 QST PAGE 51 BT I H...
```

Note the speed setting: the decoder must be at the *character* speed (14), not
the advertised 5.

## Design notes

* **Lines are emitted by length, not by transmission boundary.** A line is
  written once the text reaches `line_target_chars` (60 characters of decoded
  text, which renders to about 150 with timestamp and metadata), so lines are
  consistently long. A transmission gap no longer ends a line; if copy stops, the
  remnant is flushed after `line_idle_flush_s` (120 s) or at shutdown.
* **The target counts raw text, not the rendered line.** Glosses add roughly
  10-15 % (`speed[WPM]`, `confirm[QSL]`), so a 75-character target
  renders near 90; 60 is what lands the whole line at ~150. If you change
  `translate`, re-check the rendered length rather than trusting the target.
* `max_line_chars` (120) is the cap on *decoded* characters; `log_line_max`
  (150) is a hard cap on the finished line. If the glossed text will not fit,
  the record is split at a token boundary and the remainder is logged as **its
  own record with the same timestamp** (adds `continued:1` in the JSONL) rather
  than as an indented fragment. A gloss is never split from its meaning. Set
  `log_line_max=0` to disable.
* `line_target_chars` (60) is how much *raw* text to accumulate before writing a
  line, and `line_target_auto` lets the logger shorten that target whenever a
  line actually overflows, then creep back up - glosses can double the rendered
  width, so a fixed target straddles the cap. Set `line_target_chars=0` to go
  back to gap-based lines.
* Text that is cut by the length target is marked: a trailing `...` means more
  copy followed in the same over, a leading `...` means this line continues the
  previous one. The line's timestamp is always when its first character arrived.
* fldigi emits characters as they decode and inserts newlines at arbitrary
  buffer points, so morse-logger ignores its newlines entirely and assembles its own
  lines. `line_gap_s` (default 2 s) is still used to decide whether a burst
  belongs to the same transmission (it gates the speed watchdog, so a speed
  change never cuts a line). One continuous CQ loop
  therefore becomes one log line, not fifty fragments.
* The first character of a transmission is sometimes lost or mangled — the
  decoder needs a moment to lock onto the signal. This is inherent to CW
  decoding, not a bug; real operators repeat their call.
* fldigi is started on its own Xvfb display with a minimal, generated
  `fldigi_def.xml`; the audio device is pinned by *name* (not index), so it
  survives device renumbering.
* The daemon restarts fldigi with backoff if it dies and never replays text it
  has already logged.

## Bundling

`refresh-bundle.sh` resolves the full dependency closure with
`apt-cache depends --recurse`, downloads it, copies the app, writes
`checksums.sha256` and stamps `os-release` so `install.sh` can warn when the
bundle is being used on a different Ubuntu release. Keep the bundle directory on
a USB stick with the radio — that is the disaster-recovery copy.

