#!/bin/sh
# Put everything an offline machine needs onto a USB stick.
#
#   ./make-usb.sh /media/you/USB
#
# The stick must be writable and can be FAT32/ext4. It ends up with an OSTree
# repo holding the app AND the runtimes it depends on, so the offline machine
# needs no network at all.
set -eu
MNT=${1:-}
[ -n "$MNT" ] || { echo "usage: $0 /media/you/USB" >&2; exit 2; }
[ -d "$MNT" ] || { echo "$MNT is not a directory" >&2; exit 2; }

APP=io.github.cdevidal.MorseLogger
PLATFORM=org.freedesktop.Platform/x86_64/24.08

echo "== copying the app and its runtime onto $MNT =="
flatpak create-usb "$MNT" "$APP"
flatpak create-usb "$MNT" "$PLATFORM"

cat <<EOF

Done. On the offline machine:

  # once, if flatpak is not installed there: install it from the distro packages
  # (see README.md here - apt/dnf download recipes)

  flatpak remote-add --user --if-not-exists flathub \\
      https://dl.flathub.org/repo/flathub.flatpakrepo   # only for the name; no network
  flatpak install --user --sideload-repo="$MNT/.ostree/repo" $APP
  flatpak run $APP status

If create-usb complained about a missing runtime, copy that one too:

  flatpak create-usb "$MNT" org.freedesktop.Sdk/x86_64/24.08
EOF
