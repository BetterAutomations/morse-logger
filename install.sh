#!/usr/bin/env bash
# morse-logger offline installer.
#
# Installs everything needed to decode and log Morse (CW) on a machine with NO
# network connection: the fldigi CW decoder and its library closure, a virtual
# X server (fldigi is a GUI program; it runs headless on Xvfb), pulse/pipewire
# CLI tools, and the morse-logger logger itself.
#
#   sudo ./install.sh              install
#   ./install.sh --check           verify an existing install, change nothing
#   sudo ./install.sh --uninstall  remove the program (logs are kept)
#   sudo ./install.sh --selftest   install, then decode a synthetic CW signal
#   sudo ./install.sh --suite=N    force debs/N (default: this release's codename)
#
# Nothing is downloaded. Everything comes from ./debs/<codename>/.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)

# This script lives inside the bundle, next to ./debs and ./app. It is often
# run from the parent (project) directory by mistake, so look one level down
# and use that if it looks like a bundle.
if [[ ! -d "$HERE/debs" && -d "$HERE/bundle/debs" ]]; then
  echo "note: using bundle in $HERE/bundle"
  HERE="$HERE/bundle"
fi

PREFIX=${MORSE_LOGGER_PREFIX:-/opt/morse-logger}
BINDIR=/usr/local/bin
CHECK=0
UNINSTALL=0
SELFTEST=0
WANT_SUITE=

for arg in "$@"; do
  case "$arg" in
    --check) CHECK=1 ;;
    --uninstall) UNINSTALL=1 ;;
    --selftest) SELFTEST=1 ;;
    --help|-h) sed -n '2,20p' "$0"; exit 0 ;;
    --suite=*) WANT_SUITE=${arg#--suite=} ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

say()  { printf '%s\n' "$*"; }
ok()   { printf '  [ok]   %s\n' "$*"; }
warn() { printf '  [warn] %s\n' "$*"; }
die()  { printf '  [FAIL] %s\n' "$*" >&2; exit 1; }

as_root() {
  if [[ $EUID -eq 0 ]]; then "$@"; else sudo "$@"; fi
}

# ------------------------------------------------------------------ checks
check_install() {
  local bad=0
  say "Verifying morse-logger installation at $PREFIX"
  [[ -x /usr/bin/fldigi ]] && ok "fldigi binary" || { warn "fldigi binary missing"; bad=1; }
  if [[ -x /usr/bin/fldigi ]]; then
    if ldd /usr/bin/fldigi 2>/dev/null | grep -q 'not found'; then
      warn "fldigi has unresolved libraries:"; ldd /usr/bin/fldigi | grep 'not found' | sed 's/^/         /'; bad=1
    else ok "fldigi libraries resolve"; fi
  fi
  command -v Xvfb  >/dev/null && ok "Xvfb (headless display)" || { warn "Xvfb missing"; bad=1; }
  command -v pactl >/dev/null && ok "pactl (audio routing)"    || { warn "pactl missing"; bad=1; }
  command -v parec >/dev/null && ok "parec (audio capture)"    || { warn "parec missing"; bad=1; }
  command -v pw-play >/dev/null || command -v paplay >/dev/null \
      && ok "audio player" || warn "no pw-play/paplay (self test needs one)"
  command -v python3 >/dev/null && ok "python3 ($(python3 -V 2>&1))" || { warn "python3 missing"; bad=1; }
  if [[ -d "$PREFIX/morse_logger" ]]; then
    if PYTHONPATH="$PREFIX" python3 -c 'import morse_logger.cli' 2>/dev/null; then
      ok "morse-logger package imports"
    else warn "morse-logger package does not import"; bad=1; fi
  else
    warn "morse-logger not installed in $PREFIX"; bad=1
  fi
  [[ -x "$BINDIR/morse-logger" ]] && ok "morse-logger command in $BINDIR" || { warn "no $BINDIR/morse-logger"; bad=1; }

  if [[ -x "$BINDIR/morse-logger" ]]; then
    say ""
    "$BINDIR/morse-logger" doctor || true
  fi
  say ""
  if [[ $bad -eq 0 ]]; then say "Installation is complete and consistent."; else say "Problems found (see above)."; fi
  return $bad
}

# ------------------------------------------------------------------ remove
uninstall() {
  say "Removing morse-logger"
  as_root rm -f "$BINDIR/morse-logger"
  as_root rm -rf "$PREFIX"
  ok "removed $BINDIR/morse-logger and $PREFIX"
  say "Your logs and settings in ~/morse-logger were NOT touched."
  say "System packages (fldigi, xvfb, ...) were left installed."
  exit 0
}

[[ $UNINSTALL -eq 1 ]] && uninstall
[[ $CHECK -eq 1 ]] && { check_install; exit $?; }

# ------------------------------------------------------------------ bundle
say "morse-logger offline installer"
say "  bundle:  $HERE"
[[ -d "$HERE/debs" ]] || die "no debs/ directory in $HERE - is the bundle complete? (run it as: sudo ./bundle/install.sh)"
[[ -d "$HERE/app/morse_logger" ]] || die "no app/morse_logger directory in $HERE - is the bundle complete?"

# OS sanity: the per-release deb closure must match the release. A bundle built
# for another Ubuntu release still installs the packages this machine lacks, but
# it is not the supported path - say so instead of guessing silently.
host_os=unknown
host_suite=
if [[ -f /etc/os-release ]]; then
  . /etc/os-release
  host_os=${VERSION_ID:-unknown}
  host_suite=${VERSION_CODENAME:-}
  say "  target:  ${PRETTY_NAME:-unknown}  $(dpkg --print-architecture 2>/dev/null)"
fi

# --- which package set? ---------------------------------------------------------
# debs/<codename>/ is the normal layout; a flat debs/*.deb is an older bundle.
DEBDIR=
if [[ -n "$WANT_SUITE" ]]; then
  DEBDIR="$HERE/debs/$WANT_SUITE"
  [[ -d "$DEBDIR" ]] || die "no debs/$WANT_SUITE in this bundle (have: $(ls -1 "$HERE"/debs 2>/dev/null | tr '\n' ' '))"
elif [[ -n "$host_suite" && -d "$HERE/debs/$host_suite" ]]; then
  DEBDIR="$HERE/debs/$host_suite"
elif compgen -G "$HERE/debs/*.deb" >/dev/null; then
  DEBDIR="$HERE/debs"
  warn "this bundle has no per-release directory - using the flat debs/ set"
else
  mapfile -t suites < <(ls -1 "$HERE/debs" 2>/dev/null || true)
  if [[ ${#suites[@]} -eq 1 ]]; then
    DEBDIR="$HERE/debs/${suites[0]}"
    warn "bundle has packages for '${suites[0]}', this is '${host_suite:-unknown}' ($host_os)"
    warn "package versions may not match; continuing (use --suite= to pick)"
  else
    die "no packages for Ubuntu ${host_suite:-$host_os}; this bundle has: ${suites[*]:-nothing}
       rebuild it on a machine running this release: ./refresh-bundle.sh"
  fi
fi
say "  packages: debs/$(basename "$DEBDIR")"

# ------------------------------------------------------------------ integrity
# Verify before installing: a truncated .deb from a copied USB stick would
# otherwise fail deep inside dpkg with a confusing message.
if [[ -f "$HERE/checksums.sha256" ]]; then
  say ""
  say "Verifying package checksums"
  ( cd "$HERE" && sha256sum -c --quiet checksums.sha256 ) \
    || die "checksums do not match - the bundle is incomplete or was modified; re-copy it"
  ok "$(wc -l < "$HERE/checksums.sha256") files verified"
fi

# ------------------------------------------------------------------ debs
mapfile -t debs < <(ls -1 "$DEBDIR"/*.deb 2>/dev/null || true)
[[ ${#debs[@]} -gt 0 ]] || die "no .deb files in $DEBDIR - the bundle is not
       in git; build it first with ./refresh-bundle.sh (needs internet)"

say ""
say "Installing ${#debs[@]} packages (offline)"
say "  (dpkg is run repeatedly: some packages Pre-Depend on others in this set)"

# installed packages + versions, read in ONE dpkg-query call (a per-package
# query costs a process spawn each; with ~240 packages that is minutes)
declare -A INSTVER=()
load_states() {
  INSTVER=()
  while IFS=$'\t' read -r st ver pkg; do
    [[ "$st" == ii* ]] && INSTVER["$pkg"]="$ver"
  done < <(dpkg-query -W -f='${db:Status-Abbrev}\t${Version}\t${Package}\n' 2>/dev/null)
}
is_installed() { [[ -n "${INSTVER[$1]:-}" ]]; }

# package name + version of every .deb, resolved once
names=(); vers=()
for d in "${debs[@]}"; do
  names+=("$(dpkg-deb -f "$d" Package 2>/dev/null)")
  vers+=("$(dpkg-deb -f "$d" Version 2>/dev/null)")
done

for pass in 1 2 3 4 5 6; do
  load_states
  todo=(); ti=()
  for i in "${!debs[@]}"; do
    name=${names[i]}; ver=${vers[i]}
    [[ -n "$name" && -n "$ver" ]] || continue
    if is_installed "$name"; then
      cur=${INSTVER[$name]}
      # never downgrade something the machine already has
      if dpkg --compare-versions "$cur" ge "$ver" 2>/dev/null; then
        continue
      fi
    fi
    todo+=("${debs[i]}"); ti+=("$i")
  done
  [[ ${#todo[@]} -eq 0 ]] && break
  DEBIAN_FRONTEND=noninteractive as_root dpkg --force-confdef --force-confold -i "${todo[@]}" >/dev/null 2>&1 || true
done

# report anything that could not be installed, without being alarmist about
# packages a desktop already has (see the exclude list in refresh-bundle.sh)
load_states
still=()
for i in "${!debs[@]}"; do
  name=${names[i]}
  [[ -n "$name" ]] || continue
  is_installed "$name" || still+=("$name")
done
if [[ ${#still[@]} -gt 0 ]]; then
  say "  note: ${#still[@]} package(s) not installed here: ${still[*]}"
  say "        (only a problem if the verification below fails)"
fi

say "  package step done"

# ------------------------------------------------------------------ app
say ""
say "Installing morse-logger into $PREFIX"
# --- where the user's data lives -------------------------------------------------
# The program creates its own directories and config on first run; the installer
# only needs to decide *where*, migrate any pre-rename directory, and record the
# choice in the wrapper so `morse-logger` finds it without an environment file.
if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
  USER_HOME=$(getent passwd "$SUDO_USER" | cut -d: -f6)
  USER_NAME=$SUDO_USER
else
  USER_HOME=${HOME:-/root}
  USER_NAME=$(id -un)
fi
[[ -z "$USER_HOME" ]] && USER_HOME=/root

DEFAULT_ROOT="$USER_HOME/morse-logger"
LOG_ROOT=${MORSE_LOGGER_HOME:-$DEFAULT_ROOT}

if [[ $CHECK -eq 0 && $UNINSTALL -eq 0 && -t 0 ]]; then
  printf 'Morse logger data directory [%s]: ' "$DEFAULT_ROOT"
  read -r answer || true
  [[ -n "${answer:-}" ]] && LOG_ROOT=${answer/#\~/$USER_HOME}
fi
LOG_ROOT=${LOG_ROOT/#\~/$USER_HOME}

# migrate the pre-rename directory once, leaving a symlink so old paths work
OLD_ROOT="$USER_HOME/.cw-logger"
if [[ -d "$OLD_ROOT" && ! -e "$LOG_ROOT" ]]; then
  as_root mv "$OLD_ROOT" "$LOG_ROOT"
  as_root ln -s "$LOG_ROOT" "$OLD_ROOT"
  ok "moved $OLD_ROOT to $LOG_ROOT (symlink left behind)"
elif [[ -d "$OLD_ROOT" && -e "$LOG_ROOT" ]]; then
  warn "$OLD_ROOT still exists; leaving it alone (you now have two data directories)"
fi
# a config moved from the old location still points at the old log_dir
if [[ -f "$LOG_ROOT/config.json" ]]; then
  python3 - "$LOG_ROOT" <<'PY' || true
import json, os, sys
root = sys.argv[1]
p = os.path.join(root, "config.json")
cfg = json.load(open(p))
want = os.path.join(root, "logs")
if cfg.get("log_dir") and os.path.abspath(cfg["log_dir"]) != os.path.abspath(want):
    cfg["log_dir"] = want
    json.dump(cfg, open(p, "w"), indent=2)
    open(p, "a").write("\n")
    print("  fixed log_dir in config.json ->", want)
PY
fi
as_root mkdir -p "$LOG_ROOT"
if [[ $(id -u) -eq 0 && "$USER_NAME" != "root" ]]; then
  as_root chown -R "$USER_NAME":"$USER_NAME" "$LOG_ROOT" || true
fi
ok "data directory $LOG_ROOT (config and logs are created on first run)"

as_root mkdir -p "$PREFIX"
as_root rm -rf "$PREFIX/morse_logger"
as_root cp -r "$HERE/app/morse_logger" "$PREFIX/morse_logger"
[[ -f "$HERE/app/README.md" ]] && as_root cp "$HERE/app/README.md" "$PREFIX/README.md"
[[ -f "$HERE/install.sh" ]] && as_root cp "$HERE/install.sh" "$PREFIX/install.sh"
as_root find "$PREFIX/morse_logger" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
ok "copied morse-logger to $PREFIX"

as_root tee "$BINDIR/morse-logger" >/dev/null <<EOF
#!/bin/sh
# morse-logger - Morse (CW) logger
export PYTHONPATH="$PREFIX\${PYTHONPATH:+:\$PYTHONPATH}"
export MORSE_LOGGER_HOME="$LOG_ROOT"
exec python3 -m morse_logger.cli "\$@"
EOF
as_root chmod 0755 "$BINDIR/morse-logger"
if [[ -e "$BINDIR/cwlog" ]]; then
  as_root rm -f "$BINDIR/cwlog"
  ok "removed the old $BINDIR/cwlog command"
fi
ok "installed $BINDIR/morse-logger"

# desktop launcher convenience: nothing else to do - morse-logger is a CLI

# ------------------------------------------------------------------ verify
say ""
check_install || die "installation incomplete - see the problems listed above"

say ""
if [[ $SELFTEST -eq 1 ]]; then
  say "Running self test (synthetic CW through the virtual cable)"
  say ""
  "$BINDIR/morse-logger" selftest || die "self test failed"
  say ""
fi

cat <<'EOT'
Quick start (WebSDR or any audio source)
  1. morse-logger selftest            decode a synthetic signal end to end
  2. morse-logger cable up            create the virtual audio cable
  3. morse-logger route-audio --all         WebSDR/browser: send everything playing to the cable
     (only needed once per application - the logger re-routes new streams from
      the same apps automatically, so a browser reload does not deafen it)
     (morse-logger route-audio --list shows the streams; --index N moves just one.
      morse-logger route-audio --copy copies your speakers instead of moving anything.)
  4. morse-logger start --dial 7.030  start logging
  5. morse-logger tail -f             watch it decode
     note: start already ran tune --auto and wpm --auto, so you normally do not
     need them. Re-run 'morse-logger tune --auto' after retuning the receiver, and
     'morse-logger wpm' if the copy looks spaced out ("N O W", "WE E K").

Hardware RTL-SDR (optional, rtl-sdr package included)
  morse-logger scan --band 40m        survey a CW segment (reports, does not retune)
  morse-logger sdr --list             find the sticks (serial numbers)
  morse-logger sdr start 7.03 --hf    HF below 24 MHz via direct sampling (Blog V3)
  morse-logger sdr start 144.05       VHF/UHF with a generic DVB-T stick
  morse-logger sdr --status | --stop
  Note: blacklist dvb_usb_rtl28xxu if librtlsdr cannot claim the device.

Every command (full list, identical to "morse-logger --help")
  morse-logger doctor                 check the whole installation and audio setup
  morse-logger selftest               decode a synthetic CW signal end to end
  morse-logger cable up|down|status   manage the virtual audio cable
  morse-logger route-audio --list|--all     move playing audio onto the cable
  morse-logger route-audio --list-sources   hardware inputs (line in, USB)
  morse-logger route-audio --source NAME    loop such an input into the cable
  morse-logger restart                      stop and start again (keeps the dial)
  morse-logger start [--dial MHz]     start logging in the background
  morse-logger stop                   stop logging
  morse-logger status [--json]        health, tone, quality, lines logged
  morse-logger tail [-f] [filters]    read the log (filters: --min-quality,
                               --min-coherence, --min-chars, --raw)
  morse-logger tune [--auto]          set or auto-detect the decoder CW tone
  morse-logger wpm [--auto|--set N]   measure or set the sender's keying speed
  morse-logger retime FILE --play     re-space Farnsworth CW (W1AW 5 WPM) so it decodes
  morse-logger translate "TEXT"       try the CW abbreviation glossary
  morse-logger dial [MHz] [--clear]   dial frequency stamped on new lines (live)
  morse-logger sdr [FREQ]             tune a hardware RTL-SDR into the cable
  morse-logger config [--set k=v]     show or change settings
  morse-logger logs                   show the log directory
  morse-logger prune --days N         archive logs older than N days (--dry-run,
                               --delete; off unless asked)
  morse-logger level [--set] [--auto] measure the cable level and trim the source
                               volume toward -14 dBFS

Logs:  ~/morse-logger/logs/morse-YYYY-MM-DD.txt   (human)
       ~/morse-logger/logs/morse.jsonl            (machine)
       ~/morse-logger/logs/morse.sqlite3          (queryable)
EOT
say ""
say "All dependencies are installed."
say "(Audio needs a running desktop session - run 'morse-logger doctor' there.)" 
