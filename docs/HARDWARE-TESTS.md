# Real-hardware test plan

Everything here needs something the development machine cannot supply: an
RTL-SDR, a real sound card, a clean OS, or time. None of it has been run.

Record each line as pass / fail / note. Where a count matters, the expected
value is given; if you get a different one, that is a finding, not a nuisance.

Test machine: ____________________  OS: ______________  Kernel: __________
Dongle: ____________  Serial: ______  Antenna: ______  Date: ___________

---

## A. Install, offline

| # | test | command | expect |
|---|---|---|---|
| A1 | clean install | `sudo ./install.sh` on a fresh Ubuntu 26.04 | asks for the data root, installs, creates `/opt/morse-logger`, `morse-logger` on PATH |
| A2 | selftest | `sudo ./install.sh --selftest` | decode self-test passes without a network connection |
| A3 | truly offline | `docker run --rm --network none -v "$PWD/bundle:/bundle:ro" ubuntu:26.04 bash -c '/bundle/install.sh'` | completes; `morse-logger --version` works |
| A4 | custom root | `MORSE_LOGGER_HOME=/srv/stations ./install.sh` | wrapper exports that path; `morse-logger status` reports it |
| A5 | missing bundle | delete `bundle/debs`, run `install.sh` | refuses with a message naming `refresh-bundle.sh` |
| A6 | legacy cleanup | install over a machine with `/usr/local/bin/cwlog` | old command removed, `~/.cw-logger` left as a symlink or migrated |

## B. Audio plumbing (real PulseAudio / PipeWire)

| # | test | command | expect |
|---|---|---|---|
| B1 | create | `morse-logger cable up` | one sink `morse-logger_cable`, one source `morse-logger_cable_src` |
| B2 | idempotent | run B1 twice | second run changes nothing, no duplicate modules |
| B3 | monitor | `pactl list short modules \| grep loopback` | one loopback cable→speakers; you hear the cable |
| B4 | no monitor | `morse-logger cable up --no-monitor` | existing monitor loopback is **removed**, not just skipped |
| B5 | copy mode | `morse-logger route-audio --copy` | speakers' output is copied to the cable; mute/volume of speakers is followed |
| B6 | feedback refusal | `--copy` then `cable up` (monitor) | refused with exit 2, explains the 0 dBFS loop |
| B7 | line input | `morse-logger route-audio --source <alsa_input...>` | hardware input reaches the cable; input remains usable elsewhere |
| B8 | two cables | `morse-logger cable up --channels 2` | `morse-logger_cable_2` + `morse-logger_cable_src_2` appear |
| B9 | remove one | `morse-logger cable down --channels 2` | channel 2 gone, **channel 1 untouched** |
| B10 | reboot | reboot, `morse-logger status` | cable recreated by `start`; Wireplumber may briefly resurrect `cw_cable` — confirm it is not used |
| B11 | stream restore | `pactl list sink-inputs` after reboot | browser stream is where you left it, or `route-audio --all` fixes it |

## C. Browser behaviour

| # | test | expect |
|---|---|---|
| C1 | tab audio reaches the cable | `pactl list short sink-inputs` shows Chrome on `morse-logger_cable` |
| C2 | device change while playing | rebuild the cable during playback → Chrome sends digital silence; `morse-logger doctor` says so; a **full browser restart** fixes it (a reload does not) |
| C3 | several tabs | two WebSDR tabs produce one mixed Chrome stream — confirm this, since it defeats per-tab routing |
| C4 | muted tab | muting the tab leaves a stream that carries zeros; `doctor` flags "digital silence" |

## D. RTL-SDR basics

| # | test | command | expect |
|---|---|---|---|
| D1 | detection | `morse-logger sdr --list` | dongle found, serial shown, no `dvb_usb_rtl28xxu` conflict |
| D2 | PPM calibration | against a known carrier (WWV/CHU) or GSM | error < 5 ppm after `--ppm` |
| D3 | gain | `morse-logger sdr start --gain 30` then sweep 0–49.6 | note where the noise floor rises without signal rise (overload) |
| D4 | HF direct sampling | `--hf` on a Blog V3, tune 7.0 MHz | signals present; compare with Q-branch if available |
| D5 | bias tee | `--bias-tee` | measured at the connector; off by default |
| D6 | sample rates | 1.2 / 2.4 / 2.8 / 3.2 MS/s | note which rates stay stable for 10 minutes without USB errors |
| D7 | USB power | long cable / hub | no `-5` or `-9` errors in `dmesg` during a 10-minute run |

## E. `scan` (survey)

| # | test | command | expect |
|---|---|---|---|
| E1 | no dongle | `morse-logger scan --band 40m` | clear error from `rtl_power`, exit 1 |
| E2 | presets | `morse-logger scan --list-bands` | presets listed |
| E3 | real survey | `morse-logger scan --band 40m` | a few candidates with level, tone and keying |
| E4 | known signal present | tune a signal generator or a local beacon into range | it appears in the list, ranked |
| E5 | no false CW | run on a band with data modes only | candidates show "carrier, no keying", not a fabricated speed |
| E6 | resolution | compare a signal at 7.040 with one at 7.041 | both listed (2 kHz steps separate them) |
| E7 | timing | measure a survey with `--top 8 --dwell 12` | survey ≈ 3 s, each candidate ≈ dwell + 1 s |

## F. `scan --lock` (not implemented yet)

Implement only after E passes. When it exists, verify:

| # | test | expect |
|---|---|---|
| F1 | locks the real signal | `scan --band 40m --lock` ends on the station you can hear |
| F2 | refuses junk | never locks a data mode or a spur |
| F3 | restores on failure | if no candidate scores, the previous frequency and settings are restored |
| F4 | interacts with logging | the running logger resumes on the new frequency without a stale pid |
| F5 | repeatability | three runs in a row choose the same signal when conditions are stable |
| F6 | QSB | a fading signal that drops mid-dwell is not locked |

## G. Decoder accuracy

| # | test | command | expect |
|---|---|---|---|
| G1 | tone sweep | `tune --tone 500..1200` on a steady signal | quality peaks within 20 Hz of the true note; no peak at the filter edge |
| G2 | detune tolerance | offset the SDR by ±10/20/30/45 Hz | copy holds to ±30 Hz, breaks by 45 Hz |
| G3 | speed sweep | `wpm --set 12/18/25/35/45` | copy best within 1–2 WPM of the true speed |
| G4 | auto speed | `wpm --auto` ×10 on the same signal | values within ±1 WPM, no refusals |
| G5 | bandwidth | `bandwidth_hz` 100 / 250 / 500 | narrower rejects QRM, needs accurate tone |
| G6 | matched filter | fldigi config `CWMFILTLEN` = 1200/wpm | at 45 WPM the filter is short; verify copy is not smeared |
| G7 | squelch | `squelch_level` 0 / 30 / 55 | at 55 noise no longer decodes into letters; strong copy survives |
| G8 | clipping | raise the volume to 0 dBFS | `tune` and `doctor` both warn; copy degrades even though it sounds clean |
| G9 | Farnsworth | `retime` a 5 WPM spaced recording | ≥95 % decode, correct retrieval of the text |
| G10 | speed change mid-QSO | operator changes 15→25 WPM | watchdog restarts once, copy resumes within one cycle |

## H. Several conversations at once

| # | test | expect |
|---|---|---|
| H1 | two cables, one decoder | audio reaches channel 2 but nothing decodes — proves the missing second decoder |
| H2 | two WebSDR tabs | each tab on its own cable via `route-audio --index N --to K` |
| H3 | second decoder instance | run a second `fldigi` on port 7363 with `morse-logger_cable_src_2`; both logs separate |
| H4 | CPU with two | measure `top` for both fldigi processes during continuous copy |
| H5 | memory with two | RSS per instance (~200 MB each on the dev machine) |
| H6 | SDR channeliser | one `rtl_sdr` capture feeding two channels — needs numpy or `csdr`; measure CPU |

## I. Watchdogs and failure recovery

| # | test | expect |
|---|---|---|
| I1 | retune watchdog | mistune by 60 Hz and leave it: one autotune within the cooldown |
| I2 | speed watchdog | set a wrong speed; one restart, then measured value adopted |
| I3 | silence watchdog | key a carrier with no decode for 5 minutes: one autotune |
| I4 | kill fldigi | `kill -9` the fldigi pid: daemon restarts it, `status` returns to OK |
| I5 | kill Xvfb | kill the Xvfb for its display: daemon starts a fresh display, does **not** exit |
| I6 | second daemon | `morse-logger start` twice: second refuses, names the holder |
| I7 | port occupied | start a stray fldigi on 7362, then `start`: stray is reaped |
| I8 | stop kills all | `morse-logger stop` with two daemons: both die |
| I9 | 24 h soak | leave it running a day | no restart loop, RSS flat, no fd growth (`ls /proc/PID/fd \| wc -l`) |

## J. Logs

| # | test | expect |
|---|---|---|
| J1 | line limit | 500 lines | every text line ≤150 chars |
| J2 | split records | a long transmission | remainder is a separate record with `continued: 1` |
| J3 | midnight | run across 00:00 | new file, no lost or duplicated lines |
| J4 | tail -f | `tail -f` across midnight | keeps following the new file |
| J5 | fldigi debug | `tail -f --fldigi-debug` | raw line above, columns aligned |
| J6 | unresolved chars | rough copy | fldigi's lowercase appears as `*` |
| J7 | sqlite | `sqlite3 morse.sqlite3 "pragma integrity_check"` | `ok` |
| J8 | csv/jsonl round trip | compare `.txt` line count with `morse.jsonl` | same number of records |

## K. Capacity on an Intel Core 2 Duo SU9300

| # | test | expect / record |
|---|---|---|
| K1 | profile detection | `status` shows `profile slow` and the measured score |
| K2 | one decoder at 25 WPM | CPU % of fldigi + daemon, RAM total |
| K3 | one decoder at 45 WPM | same, expect higher |
| K4 | two decoders | does it keep up? record xruns (`pactl list sink-inputs` → "Underrun") |
| K5 | thermal | 30 minutes at full decode: `sensors`, clock throttling |
| K6 | survey while decoding | does a scan disturb the running decoder? |
| K7 | RAM headroom | total free with browser + decoder at the end of a day |

## L. Before publishing a Flatpak

| # | test | expect |
|---|---|---|
| L1 | no writable system paths | app writes only under `$HOME`/`$XDG_DATA_HOME` |
| L2 | bundled fldigi | runtime works without `/opt/morse-logger` |
| L3 | audio permissions | PulseAudio/PipeWire socket access under the sandbox |
| L4 | USB access | `--device=all` or udev rule needed for the RTL-SDR |
| L5 | no GUI scripting | fldigi stays headless (Xvfb inside the sandbox) |

