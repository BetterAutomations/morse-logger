#!/usr/bin/env bash
# Refresh the offline bundle: download every package the installer needs into
# bundle/debs/, copy the app source in, and rewrite checksums.
#
# Run this on a machine running the SAME Ubuntu release as the target, while it
# has internet.  Re-run it after an OS upgrade so the bundle matches the target.
#
#   ./refresh-bundle.sh            # normal refresh
#   ./refresh-bundle.sh --force    # re-download everything
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
bundle="$here/bundle"
force=0
[[ "${1:-}" == "--force" ]] && force=1

pkgs=(
  # the CW decoder
  fldigi
  # headless X server for fldigi
  xvfb
  # pactl / parec (virtual cable + capture)
  pulseaudio-utils
  # pw-play (self test / feeding audio)
  pipewire-bin
  # interpreter + full stdlib (python3-minimal alone lacks xmlrpc/sqlite3)
  python3-minimal
  "libpython$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')-stdlib"
  # hardware SDR support: rtl_fm / rtl_test / rtl_biast + librtlsdr
  rtl-sdr
)

echo "== target =="
. /etc/os-release
echo "   $PRETTY_NAME  ($(dpkg --print-architecture))  python $(python3 -V 2>&1 | cut -d' ' -f2)"

mkdir -p "$bundle/debs"
cd "$bundle/debs"
[[ $force -eq 1 ]] && rm -f ./*.deb

# Packages we deliberately do NOT bundle: they only arrive as dependencies of
# pipewire-bin's session bits, cannot be configured without a live init (they
# hang or fail inside a container), and are always already present on a real
# Ubuntu desktop. fldigi itself needs none of them.
exclude='^(systemd|systemd-sysv|systemd-standalone-sysusers|systemd-timesyncd|libpam-systemd|libnss-systemd|dbus|dbus-bin|dbus-broker|dbus-daemon|dbus-session-bus-common|dbus-system-bus-common|dbus-user-session|dbus-x11|libjack0|jackd2)$'

echo "== resolving dependency closure =="
mapfile -t closure < <(
  apt-cache depends --recurse --no-recommends --no-suggests --no-conflicts \
      --no-breaks --no-replaces --no-enhances "${pkgs[@]}" 2>/dev/null \
    | grep -vE '^[[:space:]]' | grep -vE '<' | sort -u | grep -vE "$exclude"
)
echo "   ${#closure[@]} packages"

# drop .deb files for excluded packages that an older run may have downloaded
for f in ./*.deb; do
  [[ -e "$f" ]] || continue
  n=$(dpkg-deb -f "$f" Package 2>/dev/null || true)
  [[ "$n" =~ $exclude ]] && { echo "   pruning $n"; rm -f "$f"; }
done

echo "== downloading =="
bad=()
for p in "${closure[@]}"; do
  # keep an existing good copy unless --force
  existing=$(ls -1 "${p}"_*.deb 2>/dev/null | head -1 || true)
  if [[ -n "$existing" && $force -eq 0 ]]; then
    if dpkg-deb --info "$existing" >/dev/null 2>&1; then
      continue
    fi
    rm -f "$existing"
  fi
  if ! apt-get download "$p" >/dev/null 2>&1; then
    # virtual packages (or arch-less ones) simply have no .deb; try once more
    if ! apt-get download "$p" >/dev/null 2>&1; then
      bad+=("$p")
    fi
  fi
done

# drop truncated downloads (apt-get can leave partial files on flaky links)
for f in ./*.deb; do
  dpkg-deb --info "$f" >/dev/null 2>&1 || { echo "   removing corrupt: $f"; rm -f "$f"; }
done

if [[ ${#bad[@]} -gt 0 ]]; then
  echo "   note: no .deb for: ${bad[*]}"
fi

echo "== bundling application =="
rm -rf "$bundle/app"
mkdir -p "$bundle/app"
cp -r "$here/morse_logger" "$bundle/app/"
cp "$here/README.md" "$bundle/app/" 2>/dev/null || true
cp "$here/install.sh" "$bundle/install.sh"
cp /etc/os-release "$bundle/os-release"
chmod +x "$bundle/install.sh"

echo "== checksums =="
cd "$bundle"
{
  sha256sum debs/*.deb
  sha256sum install.sh os-release
  find app -type f -name '*.py' -print0 | sort -z | xargs -0 sha256sum
} > checksums.sha256
sha256sum -c checksums.sha256 >/dev/null && echo "   verified $(wc -l < checksums.sha256) files"

echo "== summary =="
printf '   packages : %d .deb, %s\n' "$(ls -1 debs/*.deb | wc -l)" "$(du -sh debs | cut -f1)"
printf '   bundle   : %s total\n' "$(du -sh "$bundle" | cut -f1)"
printf '   location : %s\n' "$bundle"
