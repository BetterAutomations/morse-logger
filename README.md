# morse-logger — Morse (CW) logger

* Lightweight command-line wrapper for fldigi for unattended recording of
CW conversations.
* Listens to an audio input, decodes Morse code and files every transmission
into a timestamped log.
* Supports any audio source — WebSDR, RTL-SDR, or a receiver's line out
* No GUI required
* Tunes and measures speed by itself, then keeps them right while you sleep
* Tells you why it isn't working: level in dBFS, clipping, digital silence,
stale directories
* Diagnostics for optimal copy (decoding)
* Writes a searchable record: timestamped text, JSONL, and SQLite with
frequency, quality, tone and speed
* RTL-SDR band survey (`scan`) reports which signals are actually keying
* Multiple simultaneous audio channels, one cable per conversation. (Second
decoder is still in progress.)
* Fully offline installer, requires no network to install and use
* Optional abbreviation expansion (e.g. TU shows as 'thank you[TU]' in logs)
* Optional automatic retuning of the audio signal to isolate the best signal
* Optional automatic detection of word count (WPM) for coherent logging
* Automatic CPU detection for slower computers such as an Intel Core 2 Duo
SU9300 (released in 2008).

Requires Ubuntu 26.04 LTS for the offline installer. On other distributions use
the Flatpak in [`flatpak/`](flatpak/README.md), which also works offline
(single-file bundle or a USB repository).

Built with AI, so there might be bugs. The name 'cw-logger' was already taken,
so it is called morse-logger :-)

## Quick start

    # Open a WebSDR.org link and get clear audio playing from a CW conversation.
    sudo ./install.sh                 # Asks where the data directory goes
    morse-logger route-audio --all    # Send whatever is playing to the cable
    morse-logger start --dial 7.035   # Find the tone, measure the speed, start logging.
                                      # The --dial flag places the frequency in the logs.
    morse-logger tail -f              # Watch it decode
    morse-logger status               # Tone, speed, quality, lines so far
    morse-logger stop

That is the whole WebSDR workflow. `start` runs the tuning and the speed
measurement itself, so there is no manual setup between those two lines; `--dial`
is only a label stamped on each line, it does not retune anything.

Everything lives in `~/morse-logger` (config, `logs/`, `run/`) unless the
installer was told otherwise. `MORSE_LOGGER_HOME` overrides it.

No hardware is needed for a WebSDR. For a receiver or an RTL-SDR stick, see
[Hardware](#hardware) below.

Architecture, measurements and bundle construction are in
[`docs/DESIGN.md`](docs/DESIGN.md).

## Getting good copy (decoding)

The decoder only sees what reaches the audio; almost every "it sounds clear but
the log is garbage" case is one of these five things.

**Level: aim for −20 to −10 dBFS.** Too quiet and there is nothing to measure;
at 0 dBFS the tops of the marks are flattened and the intermodulation lands
inside the filter. `morse-logger doctor` prints the level with a verdict, and
`tune` refuses to lock on a clipped signal.

**Filter: at least as wide as the speed in WPM.** A 33 Hz SDR filter is ideal up
to about 20 WPM; a 45 WPM dot is 27 ms, and a 33 Hz filter smears it into the
next element. Open it to 200–250 Hz for contest speeds.

**No AGC.** AGC lifts the noise floor between elements and flattens the on/off
contrast the decoder slices on — the single most damaging setting for CW.

**One signal at a time.** fldigi decodes one tone. A second station closer than
the decoder bandwidth (150 Hz by default) is interleaved into the copy, so let
the receiver's filter choose.

**Retune in the receiver, then re-tune the decoder.** Changing frequency in the
browser or the dongle changes the audio pitch; run `tune --auto` (and
`wpm --auto` if the speed differs) afterwards.

### Signs of trouble

| symptom | meaning | fix |
|---|---|---|
| `q=` high, `coh=` low | the decoder is slicing noise | check level and tone; narrow the filter |
| `q= 0.0` with `OK` | decoder healthy, nothing to decode | is anything playing into the cable? |
| `tune` says "clipped at 0 dBFS" | volume too high | back it off to −12 dBFS |
| `wpm` says "very quiet" | signal too weak to measure | raise volume or RF gain |
| letters split (`T E E E`) | speed or filter mismatch | `wpm --auto`, then widen the filter |
| copy stops when the browser reloads | Chrome lost the audio device | restart the browser fully, not the tab |

`morse-logger tail -f --fldigi-debug` shows fldigi's raw characters above each
logged line, which separates "the decoder is struggling" from "our rendering is
odd".

### Hardware SDR

- **Calibrate PPM** against WWV/CHU or a known carrier before judging copy; a
  few ppm at 7 MHz is hundreds of Hz.
- **Gain**: raise it until the noise floor climbs, then back off one step. Too
  much gain is worse than too little — it produces the intermodulation that
  looks like CW.
- **HF on a Blog V3** needs `--hf` (direct sampling); the Q branch is quieter.
- **USB power**: long cables and unpowered hubs cause dropped samples that sound
  like QSB.
- **Sample rate**: 1.2 or 2.4 MS/s is enough for a single 3 kHz channel; higher
  rates only add USB load.

## Install (offline)

The bundle directory contains everything: ~240 `.deb` files (fldigi, Xvfb,
pulse/pipewire tools, python3 + stdlib) and the application.

    sudo ./install.sh              # install; no network needed
    ./install.sh --check           # verify an install, change nothing
    sudo ./install.sh --selftest   # install, then decode a synthetic signal
    sudo ./install.sh --uninstall  # remove the program (logs are kept)

`install.sh` runs `dpkg` in repeated passes (several packages Pre-Depend on
others in the same set), never downgrades what is already installed, copies the
app to `/opt/morse-logger`, writes `/usr/local/bin/morse-logger`, and finishes by
verifying every binary, library and import. It refuses to print success if the
dependency layer is not actually working.

Tested in a bare `ubuntu:26.04` container with networking disabled:

    docker run --rm --network none -v "$PWD/bundle:/bundle:ro" ubuntu:26.04 \
        bash /bundle/install.sh

To refresh the bundle after an OS upgrade (needs internet, same release):

    ./refresh-bundle.sh            # re-downloads the closure, rewrites checksums

`runtime/` is an unpacked copy of the same `.deb` set, used so you can run the
logger straight from this directory without root (`python3 -m morse_logger.cli doctor`);
`install.sh` does not use it.

## What is in the repository

```
morse_logger/        the program (standard library only - no pip, no venv)
tools/audit_verbs.py checks that every verb is documented
docs/DESIGN.md       architecture, measurements, bundle construction (not needed to use it)
docs/HARDWARE-TESTS.md  the checks that need a real dongle, sound card or clean machine
install.sh           offline installer (self-locating; prompts for the data directory)
refresh-bundle.sh    rebuilds bundle/ from the network
bundle/   runtime/   NOT in git: ~340 MB of .deb files and an unpacked copy of
                     them. Rebuild with ./refresh-bundle.sh when you need them.
test_recordings/     NOT in git beyond the directory: the W1AW audio is large and
                     is ARRL material. Drop your own .mp3/.txt pairs there to use
                     `morse-logger retime` against a known text.
```

Running it from a checkout, with no root and no bundle, only needs fldigi on the
`PATH` (or in `runtime/`): `python3 -m morse_logger.cli doctor`.

## Routing audio into the cable

`route-audio --all` is the usual choice: it moves every playing stream onto the
cable, which is right when the browser is the only thing making noise. Four ways
in, ordered by how much they disturb:

| command | effect | use when |
|---|---|---|
| `morse-logger route-audio --copy` | copies whatever plays on your speakers into the cable | nothing is rerouted and no app has to be remembered; but it copies *everything* and follows your speaker volume |
| `morse-logger route-audio --all` | moves every playing stream to the cable | the WebSDR is the only thing playing |
| `morse-logger route-audio --list` then `morse-logger route-audio --index N` | moves that one stream | you want the browser only and music left on the speakers |
| `morse-logger route-audio --source NAME` | loops a **hardware input** (line in, aux, USB sound card) into the cable | a receiver is wired into the sound card - see `--list-sources` |

`--list` shows **playback streams**: the applications currently producing sound
(a browser tab, a media player). A program that is running but silent does not
appear, and this is not a process list. `--list-sources` is the opposite
direction: the capture devices you could record from.

**Copy and move cannot be combined**, and neither can a copy with monitoring:
one loop sends your speakers into the cable, the other sends the cable into your
speakers, and together they run away to 0 dBFS. Whichever combination you ask
for, morse-logger refuses with exit 2 and tells you the two ways to resolve it,
rather than guessing.

**Routing sticks after the first time.** `morse-logger route-audio` remembers which
applications it routed (in `route_apps`) and the running logger re-routes any new
stream from those applications every few seconds. That matters because
PulseAudio/PipeWire forgets a moved stream when the stream ends: reloading the
WebSDR tab, restarting the browser, or letting the page go idle puts the audio
back on your speakers and the logger goes silently deaf. With the watcher you
route once per application, not once per session. `morse-logger config --set
route_watch=false` turns it off; `morse-logger config --set route_apps=[]` forgets them.

A moved stream is looped back to your speakers by default, so you still hear it.
`route-audio` never moves morse-logger's own monitor loopback: that would feed the
cable into itself and pin the input at 0 dBFS.

`--dial` is only a label: it never retunes anything. It is written with each
logged line so the log says what you were listening to. Because you will retune
mid-session, the value can be changed live without restarting:

    morse-logger dial 3.545     # from now on, new lines are stamped 3.545MHz
    morse-logger dial           # show the current value
    morse-logger dial --clear   # stop stamping a frequency

The daemon re-reads it for every transmission (a small control file in
`~/morse-logger/run/dial`), and it is also saved as the default for the next
start.

Any CW note between ~300 and ~2600 Hz works — `morse-logger tune --auto` measures the
audio and moves the decoder; you do not have to hit a magic pitch.

## Surveying a band (RTL-SDR)

`scan` finds out what is in a CW segment without disturbing anything:

```
$ morse-logger scan --band 40m
40m CW (7.000-7.125 MHz)
  surveying 40m CW (7.000-7.125 MHz) with rtl_power (3 s)
    probing 7.0330 MHz  (peak -62 dB, +33 dB above neighbours) - 8 s
    probing 7.0510 MHz  (peak -78 dB, +17 dB above neighbours) - 8 s

   #   frequency      level    tone   keying
  --------------------------------------------------------
   1       7.0330    -9 dBFS    700 Hz   20.3 WPM (conf 0.94)
   2       7.0510   -26 dBFS    880 Hz   carrier, no keying

  survey only - nothing was retuned. To work one of these:
    morse-logger sdr start --freq 7.0330 --mode usb
    morse-logger tune --tone 700
    morse-logger wpm --set 20
```

Two stages: `rtl_power` sweeps the segment in a few seconds (that is what makes
it fast - stepping `rtl_fm` across 125 kHz would take minutes), then `rtl_fm`
dwells on the few best candidates. A candidate is a *cluster* of bins that is
narrow (a wide rise is a data mode or an overloaded front end) and stands clear
of the noise around it. The dwell is what separates CW from everything else: a
carrier with no keying is reported as exactly that.

Rejected outright: plateau edges (a cliff on one side only), carriers inside the
noise floor, and anything wider than ~5 bins. **Nothing is retuned** - if a
logger is running it keeps logging. Presets are `80m 40m 30m 20m 17m 15m 10m`,
or use `--start 7030 --stop 7060` in kHz. `--top` sets how many candidates are
probed and `--dwell` how long each gets.

`--lock`, which would tune and set the speed on the best candidate, is not
implemented yet.

## Real hardware

    morse-logger config --set input_device=<pulse-source-name>
    morse-logger start

`morse-logger cable status` lists every sink and source. For a USB sound card that is
something like `alsa_input.usb-...-analog-stereo`; the virtual cable is then
unnecessary. Feed the receiver's audio out (or the headphone jack) into the
sound card's line/mic input, set the level so the waterfall is not clipping, and
run `morse-logger tune --auto`.

## Commands

| command | meaning |
|---|---|
| `morse-logger doctor` | check the whole installation and audio setup |
| `morse-logger selftest` | decode a synthetic CW signal end to end |
| `morse-logger cable up\|down\|status` | manage the virtual audio cable (`--no-monitor`) |
| `morse-logger route-audio [--list\|--all\|--index N\|--copy\|--source NAME]` | get audio onto the cable: move streams, copy the speakers, or loop a hardware input |
| `morse-logger start [--dial MHz] [--tone Hz]` | start logging in the background |
| `morse-logger restart` | stop and start again, keeping the dial |
| `morse-logger stop` / `morse-logger status [--json]` | lifecycle and health |
| `morse-logger tune [--auto\|--tone Hz]` | set the decoder CW tone |
| `morse-logger dial [MHz] [--clear]` | show/set the dial frequency stamped on new lines, **live** |
| `morse-logger wpm [--auto\|--set N]` | measure the fist's speed, or set it - applied live |
| `morse-logger retime FILE [--play]` | re-space Farnsworth CW so the decoder can read it |
| `morse-logger sdr [FREQ] [--hf\|--mode\|--device\|--ppm\|--gain\|--bias-tee]` | tune a hardware RTL-SDR into the cable |
| `morse-logger sdr --list \| --status \| --stop` | enumerate sticks, check, stop |
| `morse-logger tail [-n N] [-f] [--date YYYY-MM-DD]` | read the log |
| `morse-logger translate "TEXT"` | try the CW abbreviation glossary on any text |
| `morse-logger logs` | show the log directory |
| `morse-logger config [--set key=value]` | show/change settings |

## Several conversations at once

One decoder copies one signal, so N conversations means N cables and N decoders.

    morse-logger cable up --channels 2          # also creates morse-logger_cable_2
    morse-logger route-audio --index 3 --to 2   # this tab onto the second cable

Each cable has its own sink and source (`morse-logger_cable_2`,
`morse-logger_cable_src_2`) and is independent: `cable down --channels 2` removes
only that one. Channel 1 keeps the original names.

For the SDR, `--channels` is necessary but not sufficient: one dongle has one
tuner, so several simultaneous signals need one wideband capture plus a software
channeliser.

## Logs

`~/morse-logger/logs/` (override with `MORSE_LOGGER_HOME`):

* `morse-YYYY-MM-DD.txt` — one line per write, timestamped at its first
  character. A line normally holds one over, but copy is accumulated until it is
  long enough, so one line can span two short overs and a long over can be split
  in two (see [Design notes](#design-notes)):

      2026-11-23 11:23:00  [  3.555MHz q= 65.2 coh=100  760Hz     ]  E QSL EARL K 4EIE U DE W1QJB
      2026-11-23 11:23:00  [       --- q=    - coh=  -    -Hz     ]  T W H A T ARE YOUR PLANS
      2026-11-23 11:23:00  [ 144.05MHz q= 12.0 coh=  9 1500Hz  x12]  CQ CQ CQ DE K4EIE K

  The bracketed block comes first, right after the timestamp, in **fixed-width
  fields**, so the decoded text always starts in the same column (63 here)
  whatever is known or missing. Unknown values are dashes, not omissions:

  | field | width | meaning |
  |---|---|---|
  | dial | 10, right-aligned | frequency from `morse-logger dial`, `---` if unset |
  | `q=` | 5, one decimal | fldigi's peak decoder quality, 0–100 (tone, not text) |
  | `coh=` | 3 | coherence of the decoded text, 0–100 (how much looks like real words) |
  | tone | 4 + `Hz` | the decoder's CW tone |
  | repeat | 4 | `x3` when a transmission repeats, `999+` beyond that, else blanks |

  Which makes the text a clean column for `cut -c64-`, `awk`, or just reading.

* `morse.jsonl` — same records plus start/end timestamps, repeat counts and the
  exact quality sample; one JSON object per line.
* `morse.sqlite3` — table `captures` for queries:
  `sqlite3 morse.sqlite3 "select text from captures where text like '%CQ%'"`

Repeated identical transmissions (e.g. an unattended CQ loop) are marked `xN`
instead of flooding the log.

<a name="hardware"></a>
## Hardware RTL-SDR

The decoder only needs audio, so hardware support is a single extra step: run
`rtl_fm` and pipe it into the cable. `morse-logger sdr` does exactly that — no SDR GUI,
and the SDR is not required for WebSDR operation.

| stick | range | notes |
|---|---|---|
| generic DVB-T (R820T/R820T2) | ~24 MHz – 1.7 GHz | 10 m and up. **No HF** — the tuner simply cannot tune below 24 MHz. |
| RTL-SDR Blog V3 | 24 MHz – 1.7 GHz, **plus HF <24 MHz** | HF works by direct sampling (`-E direct2`), bypassing the tuner; also has a bias tee (`-T`). |

```
morse-logger sdr --list                      # which sticks, and their serial numbers
morse-logger sdr start 7.03 --hf             # 40 m CW on the Blog V3 (direct sampling)
morse-logger sdr start 144.05 --gain 30      # 2 m on the generic stick
morse-logger sdr start 10.11 --mode lsb      # below the 30 m band edge, LSB convention
morse-logger sdr --status | --stop
```

With two sticks, pick one by serial — that is stable across replugs, unlike the
index:

```
morse-logger sdr start 7.03 --hf --device 00000001
```

Options: `--mode usb|lsb|am` (usb/lsb for CW), `--device`, `--ppm` (frequency
correction), `--gain` dB or `auto`, `--sample-rate` (24000 default), `--hf`,
`--bias-tee`.

Recommended sequence:

```
morse-logger start --dial 7.03
morse-logger sdr start 7.03 --hf
morse-logger tune --auto        # the note sits wherever your tuning error puts it
morse-logger wpm                # then set the fist speed
```

Both watchdogs keep working: a drifting note is re-found, and a stubborn speed
mismatch triggers a decoder restart.

**Stray feeds.** Anything playing into `morse-logger_cable` is decoded and logged as if it
were radio, including a forgotten test feed. `morse-logger doctor` lists what is playing
into the cable and warns when a player is attached with no tracked SDR process;
`morse-logger sdr --status` reports feeds started from *other* `MORSE_LOGGER_HOME`s, and
`morse-logger sdr --stop` terminates them too. That gap is not theoretical: a test feed
once ran unnoticed for 40 minutes and logged its own test signal as real copy.

**If `rtl_fm` says "No supported devices"**, the kernel's DVB driver has claimed
the stick. Blacklist it and replug:

```
echo 'blacklist dvb_usb_rtl28xxu' | sudo tee /etc/modprobe.d/blacklist-rtl.conf
sudo modprobe -r dvb_usb_rtl28xxu
```

**Calibrate `--ppm` once per stick** with `rtl_test -p` (see
`https://www.rtl-sdr.com/rtl-sdr-quick-start-guide/`); a cheap stick can be 30–60
ppm off, which is 200–450 Hz at 7 MHz — enough to put a CW signal outside even
a 250 Hz filter.

**One instance per machine.** All instances share the `morse-logger_cable` sink/source and
the XML-RPC port (7362), so a second `morse-logger start` fails with
`xmlrpc port 7362 is already in use`. That is deliberate — it stops two decoders
fighting over one cable — but it matters when testing: give a test home its own
`xmlrpc_port` and its own sink, or stop the live logger first.

## Abbreviation glossary

Decoded CW is full of shorthand, so each line in the text log is annotated in
place: the **readable text first**, the token that was actually sent in
**brackets**, so a wrong expansion is always visible right next to it.

```
calling any station[CQ] CQ CQ from[DE] K4EIE go ahead[K]
your[UR] readability-strength-tone report[RST] 599 location[QTH] PALATKA and[ES] weather[WX]
E acknowledge / confirm[QSL] EARL go ahead[K?] 4EIE you[U] from[DE] W1QJB semi-automatic key[BUG]
```

* `word[TOKEN]` - expansion, then the token that was sent.
* `word[TOKEN?]` - the token has more than one plausible reading and the context
  test could not settle it. Read the token: `from[DE?]` in a state QSO party
  exchange means **Delaware**, not "from"; `with[W?]` after `100` means watts.
* `<BT>` is structural: the sender's paragraph break ends the log line, and the
  marker stays on it (`... CARRY [<BT>]`). The next line is a new paragraph. A
  `<BT>` with almost nothing before it stays inline, since that is far more
  likely a misread than a paragraph break. Turn it off with
  `morse-logger config --set break_on_bt=false`.
* A token repeated in a run is expanded once - `calling any station[CQ] CQ CQ`,
  not three times, because `CQ CQ CQ` is one call.
* Every daily log file starts with a one-line legend.

Abbreviations deliberately **not** expanded: callsigns, RST numbers, and any word
that is simply English. Around 160 entries cover Q-signals (`QTH`, `QSL`, `QSY`,
`QRZ`, `QRM`, `QRP`, ...), operating shorthand (`UR`, `ES`, `HI`, `FB`, `TNX`,
`PSE`, `AGN`, `CUL`, `CUAGN`), station bits (`RIG`, `ANT`, `ATU`, `BUG`, `PADDLE`,
`KEYER`), and letters (`R`, `K`, `DE`, `U`, `W`, `X`, `SK`, `AR`, `KN`).


## Finding the CW tone

`morse-logger tune --auto` scores candidates by **keying structure** - elements
quantised to a dot length and a dash length - and refines the winner on envelope
contrast. Picking the *steadiest* narrow-band component instead would follow a
music bed or a video, which can hold a perfectly steady tone hundreds of Hz away
from the CW. On synthetic audio with a loud
steady music tone at 700 Hz it moves to the CW at 600, 1000 or 2000 Hz correctly.
It is unreliable when the CW is within ~100 Hz of a strong steady tone, where the
analysis band cannot separate them.

## The question mark flag (`?`)

Exactly one token can still carry it, and only in one situation:

| token | situation | result |
|---|---|---|
| `R` | directly beside digits (`599 R`) | `received[R?]` - could be "roger" or part of a group |
| anything else | - | never flagged |

Five tokens are resolved by context rather than flagged:

| token | rule | result |
|---|---|---|
| `DE` | always "from"; it is the callsign separator | `from[DE]` |
| `W` | after a number it is watts, otherwise "with" | `watts[W]` / `with[W]` |
| `GA` | closing an over it is the invitation, otherwise a greeting | `go ahead[GA]` / `good afternoon[GA]` |
| `K` | suppressed inside a split callsign, confident at the end of an over | `EARL K 4EIE`, `go ahead[K]` |
| `X` | suppressed between numbers (contest multiplier) | `2 X 3` untouched, else `bye[X]` |

"Suppressed" means the token is left exactly as sent - `EARL K 4EIE` stays
`EARL K 4EIE` rather than becoming `EARL go ahead[K?] 4EIE`, which is misleading
in a log full of callsigns.

Drop the flag entirely with `morse-logger config --set translate_uncertain=false`, or
preview without it: `morse-logger translate --no-marks "599 DE K"`.

The argument for keeping it: with the readable text now in front, a wrong
expansion is the *primary* thing you read, so it is far easier to skim past than
it was when the abbreviation led the line. The flag costs one character and turns
an unverifiable claim into a checkable one:

```
grep -E '\[[A-Z]+\?\]' morse-2026-11-23.txt   # every uncertain reading, nothing else
```

Context rules that earn the flag's absence: `DE` between callsigns is "from";
`K` at the end of a transmission is the invitation; `R` away from digits is
"received"; `X` at the end is "bye". Everything else stays flagged.

## Decoder settings that matter

fldigi's CW decoder has four knobs that change what actually comes out, and the
tool sets all of them:

| setting | value | why |
|---|---|---|
| squelch | on, level 30 | with the squelch off fldigi decodes the noise floor into letters. `squelch_level` raises the gate to reject weaker QRM, lowers it to hear weak signals |
| matched filter | on, `1200/wpm` ms | the filter should average about one dot. It used to be fixed at 100 ms, which spans three and a half dots at 45 WPM and smears the transitions the decoder slices on |
| bandwidth | 150 Hz (`bandwidth_hz`) | ±75 Hz around the tone. Widen to 250 Hz for 40+ WPM contest copy, narrow to 100 Hz when a neighbour is inside the filter |
| sweet spot and speed tracking | tone, on | the sweet spot is set to the tuned tone, so the decoder's reference and the filter agree |

**Clipping beats all of it.** A signal at 0 dBFS sounds perfectly clear and
decodes as garbage, because the tops of the marks are flattened and the
intermodulation lands inside the filter. `morse-logger doctor` flags a cable
level above -1.5 dBFS for that reason; aim for about -12 dBFS.

## Keeping junk out of the log

`q=` measures the *tone*, not the *text*: two stations on top of each other, or a
distorted stream, score 60 while the copy is nonsense. The log therefore also
carries a **coherence** score — the fraction of tokens that look like real text
(glossary term, callsign, short report, or an ordinary English word):

```
coh=100  CQ CQ CQ DE K4EIE K
coh=76   E QSL EARL K 4EIE U DE W1QJB GREAT BUF JOB EARL X I LOVED MY BUG
coh=40   TK OM 9 B E ER ES E S SANG 9 BE TTY A<SK> G ABT TTING A SPE 9   <- q=54.9!
coh=20   FJ B** T W EE
```

Because a four-letter run of letters is *not* evidence of a word — that is
exactly what a mis-decoded fist produces — shape alone would score garbage high.
Two thresholds use it:

| setting | default | effect |
|---|---|---|
| `gloss_min_coherence` | 45 | below this, the line is stored **raw, without brackets** (annotating garbage only makes it harder to read) |
| `min_coherence` | 0 (off) | below this, the line is kept out of the **text log** entirely |
| `min_quality` | 0 (off) | same, using `q=` |
| `min_chars` | 2 | drops scraps like `EE` |

Both scores use the same **0–100** scale, so `q=45` and `coh=45` mean comparable
things and you never have to remember which one is a fraction. 0.45 is still
accepted and read as 45, with a note; above 100 is rejected. Records written
before the field existed have their coherence computed from the text when you
tail them.

Nothing is ever discarded: JSONL and sqlite keep every line with
`coherence`, `filtered` and `quality`, so a filtered line can always be found
later. To review a night's log without the noise:

```
morse-logger tail --min-coherence 45 -n 200       # keep lines that are 45% believable or better
morse-logger tail -f --min-coherence 45 --min-chars 20   # live, junk excluded
morse-logger tail -f --fldigi-debug                      # show fldigi's own output
morse-logger tail --min-quality 40 --raw
morse-logger config --set min_coherence=45        # stop writing junk to the .txt log
```

`-f` follows either view — the plain text log, or the JSONL stream when a filter
is active — and keeps applying the same filters to each new line as it is
written. Ctrl-C or `kill` ends it cleanly.

`--fldigi-debug` prints the characters fldigi actually emitted on the line above
each entry, so you can see exactly what the decoder produced before any marking,
glossing or filtering:

```
  fldigi> Q POTA DE E77SR RT PTTA DE EI7SR IQ PMTA DE E77SR NQ
2026-11-24 09:14:40  [q=61.1 coh=26]  Q POTA DE E77SR RT PTTA DE EI7SR IQ PMTA DE E77SR NQ
```

It reads `morse.jsonl`, which is where the untouched decode is kept.

It keeps working across the things that break a naive `tail -f` on a log like
this: **the midnight rollover** (the daemon starts `morse-<next-day>.txt`, so the
filename is re-resolved on every poll), a **half-written line** (held back until
its newline arrives instead of being printed mangled and lost), a **truncated or
replaced file**, and **multibyte characters split across a read** (positions are
byte offsets, not text-mode cookies).

Measured on a real 80 m session: `q` ranged 30–65 with no useful separation,
while coherence split cleanly - readable QSO copy at 53–100, noise at 0–40.

## Unresolved characters (`*`)

Morse has no capital letters, but fldigi emits **lowercase** for characters it
only partly resolved — a one- or two-element fragment it could not turn into a
letter. It also emits a bare `*` for a character it could not resolve at all.
Both mean the same thing, so both are shown as `*` in the human-readable log:

```
Q MST ND9M * F *Q MS T D 9M * NQ ***N Q      <- text log
Q MST ND9M i F eQ MS T D 9M a NQ tiuN Q      <- morse.jsonl, raw decode
```

`?` cannot be used - CW really sends it (`..--..`) - and an asterisk is not in
the Morse alphabet, so it cannot be confused with decoded text. It also does not
collide with the log's other punctuation (`[glosses]`, `?` beside a digit for R,
`...` for a partially held line). Set `uncertain_char` to another symbol, or to
`""` to keep fldigi's letters as they arrive; the raw decode is always preserved
in `morse.jsonl`.

## When glossing starts: `gloss_min_coherence`

Abbreviations are only expanded when a line's coherence reaches
`gloss_min_coherence` - **45 of 100** by default. Below that the line is stored
and shown exactly as decoded, because expanding a glossary against noise only
produces confident-looking nonsense:

```
coh= 63   AK4ZZ de WB4IT ur rst 599 qth palatka   -> glossed
coh= 26   Q MST ND9M NQ MST ND9M i F eQ MS T      -> left raw (no expansions)
```

Coherence counts glossary terms, callsigns, short reports (`599`, `55N`) and real
dictionary words, so it rises with actual readable copy rather than with length.
It is scored per line, before glossing, on the same 0-100 scale as everything
else - `45` and `0.45` are accepted for it.

```
morse-logger config --set gloss_min_coherence=45     # default
morse-logger config --set gloss_min_coherence=0      # gloss everything
```

Separate from this, `min_coherence` (default 0) hides whole lines below a score
from the text log; the JSONL and sqlite always keep them.

## Keying speed

Not tuning — this is a **speed mismatch**. fldigi decides whether a gap is an
element gap, a letter gap or a word gap from its current WPM estimate. If it
assumes a faster fist than the one being sent, an ordinary intra-word gap looks
like a word gap and gets a space. Measured with a known-good signal:

| signal | decoder assumes 20 WPM | result |
|---|---|---|
| 12 WPM | 20 | `W W H AT ARE YOUR PLANS FOR THE WEEK` — 30 % of tokens single letters |
| 12 WPM | 12 | `T WHAT ARE YOUR PLANS FOR THE WEEK` |
| 18 WPM | 20 | `WHAT ARE YOUR PLANS FOR THE WEEK` — clean |
| 25 WPM | 20 | `WHAT ARE YOUR PLANS FOR THE WEEK` — clean |

Too fast an assumption → extra spaces. Too slow → letters merge into one word.
The decoder's own tracking (`CWTRACK`) only partly corrects it, and fldigi has
**no run-time speed control** (no XML-RPC method), so the fix is to set the speed
and restart. Measure it instead of guessing:

```
morse-logger wpm             # measure the fist now (6 s of audio)
morse-logger wpm --auto      # measure and save it
morse-logger wpm --set 15    # set it by hand and apply it to the running decoder
```

It works from the keying envelope in two passes:

1. **Broadband** (accurate) — the whole signal goes quiet between elements. This
   is the normal case, verified to ±1 % from 10 to 35 WPM.
2. **At the CW tone** (fallback) — for audio that never goes silent: a music
   bed, a voice, a YouTube video with a soundtrack. The keying envelope is
   measured at the decoder's own tone instead. It first checks that a *narrow
   carrier* actually exists there, because speech and music spread energy over
   hundreds of Hz and can look deceptively rhythmic; if the energy is spread out
   it refuses instead of inventing a number.

A window that reads as noise is retried on *fresh* audio (up to three windows),
because that verdict says "this stretch was clicks, a fade or QRM", not "there is
nothing here" - which is why re-running the same command against the same
transmitter so often succeeded on the second try.

It listens longer when the first window is short of CW - 6 s, then 16, then 31
seconds - because a slow sender or a short over can leave only a handful of
elements in any one window. Only a shortage of elements triggers the wait; noise
is rejected immediately, since more of it will not help.

It also refuses rather than lying about: noise clicks at the 12 ms floor, a
dash/dot ratio far from 3, elements that are not quantised into dot/long groups,
and dot lengths that vary by more than 35 %.

**Known hard case:** CW mixed with continuous narration. The band fallback can
still be fooled — it may report a speed from the voice's own rhythm. If the
number looks wrong for what you are hearing, set it by hand
(`morse-logger wpm --set 12`) and trust the log's coherence score: good copy means the
speed was right.

### Speed watchdog (automatic)

Because `morse-logger wpm` measures the real fist, the daemon keeps the speed
corrected by itself:

```
[11:18:37] speed watchdog: fist measures 12 WPM (dot 100 ms, confidence 0.90)
           but the decoder is set to 20 - restarting fldigi at 12 WPM
```

It measures every 60 s on a fast machine (120 s medium, 240 s slow) and restarts
fldigi only when the measured speed differs by **20 % or more and at least
2 WPM**. Guards, because a restart costs a few seconds of copy:

| guard | behaviour |
|---|---|
| no signal / noise | `wpm.measure` rejects it, nothing happens |
| low confidence (<0.5) | ignored — irregular fists and QRM do not trigger restarts |
| mid-transmission | deferred until the line ends, so no line is cut in half |
| small drift (<20 % or <2 WPM) | ignored |
| `speed_watchdog_max_restarts` | 4 per session, then it stops trying |

`morse-logger status` reports the decoder's current speed, and adds the last
measurement when it disagrees with the setting - a stale speed is visible rather
than hidden:

```
  service   fldigi pid 1627546  OK
  settings  tone 610 Hz  speed 22 WPM  (last measured 17 WPM 96s ago)
```

`service` is about the process, `settings` about the decoder. **OK** = alive
*and* answered XML-RPC at the last status write; it says nothing about the
signal, so look at `signal` for quality. **NOT RESPONDING** = the process exists
but does not answer, and the daemon is restarting it. No `service` line at all =
nothing is running.

It also reports `speed wd  N speed correction(s), decoder now X WPM` when the
watchdog has acted.
Disable with `morse-logger config --set speed_watchdog=false`.

**A probe cannot outlast its own cycle.** The speed check captures audio synchronously, so the adaptive listen (5 s, then +10, +15) is capped at the interval minus two seconds: at `speed_watchdog_interval_s=15` it listens in two windows totalling 13 s. Short cycles are viable on a fast CPU - the measurement itself costs ~0.05 s - but each probe is a blind window for text (nothing is lost, it is buffered in `textout.txt`) and it also waits for a 5.5 s gap so it never splits an over, so a 15 s cycle really means "every 15 s, between overs".

**Watchdog cycles are deliberately slow, and slower on old hardware.** The
periodic work is cheap (`autotune.pick` costs ~0.35 s of CPU for 8 s of audio on
a modern laptop), but an *intervention* is not: an autotune spends ~13 s of
captures and a speed change restarts fldigi, and during that time nothing is
decoded. A mistuned decoder only costs a few lines that the coherence filter
keeps out of the log, so reacting quickly buys almost nothing while costing the
decoder's state.

**The profile is detected, not configured.** On first run morse-logger times a
fixed integer loop (the same interpreter work the audio analysis does) and stores
the score in `run/cpu.json`, so it is measured once and never again. Model names
are not used, because a VM reports the host CPU and a throttled laptop runs far
below its rating. **Fast is the default**; the profile only moves when the
benchmark says this machine is slow. `morse-logger status` shows the result:
`cpu  profile fast (measured 0.030s), speed check every 60s`.

| profile | score | speed check | cooldown | silence | scan workspace |
|---|---|---|---|---|---|
| `fast` (default) | ≤ 0.055 s | 60 s | 600 s | 600 s | 25 Hz, refine 10 Hz |
| `medium` | ≤ 0.100 s | 120 s | 1200 s | 1200 s | 50 Hz, refine 20 Hz |
| `slow` | > 0.100 s | 240 s | 2400 s | 2400 s | 50 Hz, refine 20 Hz |

Each profile is twice as gentle as the one above it, so the speed check runs
every minute on a modern machine and every four minutes on a 1.2 GHz dual core.

A Core Ultra 9 measures ~0.030 s (fast). An old Core 2 at full clock lands near
0.08 s (medium); a 1.2 GHz Core 2 Duo SU9300 or an Atom is 0.10 s or worse and
gets `slow`. There is no setting to override it — the measurement is the honest
answer — but the underlying keys (`speed_watchdog_interval_s`,
`watchdog_cooldown_s`, `watchdog_silence_s`) are still individually settable.

Beyond the intervals, `medium` and `slow` also:

* **doubles the autotune workspace** (25 → 50 Hz search, 10 → 20 Hz refine),
  halving the band scan - the only CPU-heavy thing we do (0.35 s per scan here,
  ~1.5 s on a Penryn);
* **halves the supervisor's wake rate** (0.2 → 0.4 s), which is where the daemon's
  resting 0.7 % CPU goes;
* **raises the speed-change threshold** 20 % → 25 %, because a restart costs a
  slow machine 10–20 s of deafness.

Not scaled, deliberately: fldigi's own DSP (C++, and the one thing that must run
in real time), sqlite writes and the log follower (a line every few seconds), and
`retime` (a one-off file operation, ~6 s for 16 minutes of audio).

## What `morse-logger start` does by itself

Three things happen before you see a line, and two keep running afterwards:

| when | what |
|---|---|
| at startup | **`tune --auto`** - finds the CW note in the passband (`autotune_on_start`) |
| at startup | **`wpm --auto`** - measures the fist and sets the decoder speed *before* fldigi launches, so there is no restart (`wpm_auto_on_start`) |
| every 240 s (profile) | **speed watchdog** - re-measures; restarts the decoder if the fist drifted >20 % |
| after 6 bad lines | **retune watchdog** - strong tone + unreadable text -> re-run autotune |
| after 5 min silence | **silence watchdog** - keying present but nothing decoded -> re-run autotune |

The silence watchdog exists because the other two need *something* to reason
about: the retune watchdog counts unreadable transmissions, and the speed
watchdog needs a measurable fist, so a carrier far enough off frequency to
produce no copy at all could otherwise sit there forever. It checks the audio
itself and only fires when keying is actually present.

Log lines tell you when any of it happens:

```
[12:19:32] wpm at startup: fist measures 13 WPM (dot 92 ms, confidence 0.99); decoder was 20 WPM -> 13 WPM
[12:19:35] fldigi ready (pid 513164, display :100, input fresh_src, CW tone 900 Hz)
[12:21:10] speed watchdog: fist measures 25 WPM (dot 48 ms, confidence 0.90); decoder was 20 WPM -> 25 WPM
[12:21:10] speed watchdog: decoder restarted at 25 WPM (restart #1)
[12:26:40] watchdog: keying is present (25 WPM) but nothing has decoded in 5 min - decoder is probably off-frequency (1500 Hz), re-running autotune
```

| setting | default | effect |
|---|---|---|
| `autotune_on_start` | true | find the tone at startup |
| `wpm_auto_on_start` | true | measure the fist at startup |
| `speed_watchdog` | true | periodic speed check (60 s on a fast machine) |
| `speed_watchdog_max_restarts` | 4 | restarts per session |
| `watchdog_silence_s` | 600 | quiet time before the silence watchdog looks |

## Retune and silence watchdogs

A signal only ~45 Hz off the decoder carrier gives perfect tone quality and
gibberish text — the same signature as an unreadable band, but with a completely
different fix. So when the last **8** transmissions are all at or below **45 %**
coherence *and* all at **q >= 40**, the daemon re-runs autotune once and says so:

```
[10:55:04] watchdog: 6 unreadable transmissions in a row with good tone quality
           - the decoder is probably off-frequency (1500 Hz), re-running autotune
[10:55:04] watchdog: decoder moved to 1180 Hz (retune #1)
```

It will not thrash: one retune per 600 s at most, and the window is cleared
afterwards so it takes eight more bad transmissions to fire again. Weak signal
garbage (low q) never triggers it, and neither does clean copy. `morse-logger status`
shows how many automatic retunes happened this session.

| setting | default |
|---|---|
| `watchdog` | `true` |
| `watchdog_window` | 8 transmissions |
| `watchdog_coherence` | 45 (at or below counts as unreadable) |
| `watchdog_min_quality` | 40 |
| `watchdog_cooldown_s` | 600 |

Turn it off with `morse-logger config --set watchdog=false`.

## If `morse-logger start` says the logger did not report ready

Startup is not instantaneous: it measures the fist (~5 s), starts fldigi under
Xvfb (~4 s) and autotunes (~9 s). `morse-logger start` prints each phase as it happens
and waits up to 60 s; on failure it prints the last lines of `run/morse-logger.out`
instead of only naming the file:

```
  ... measuring the fist (0s)
  ... starting the decoder (5s)
  ... tuning the decoder (9s)
now running (pid 712785)
```

If the message appeared but the log shows `logging to ...`, the machine was just
slower than the old 40 s limit - the daemon is running, nothing is lost.

## Licence

GPL-3.0-or-later - see [LICENSE](LICENSE). This drives fldigi as a separate
process over XML-RPC rather than linking it, so the GPL is a choice rather than
an obligation; it matches the convention in ham radio software.
