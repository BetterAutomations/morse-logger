#!/bin/sh
# Build the Flatpak and leave a single-file bundle in this directory.
#
#   ./build.sh            build, bundle, and install for the current user
#   ./build.sh --no-install
#
# Needs: flatpak (1.12+), network access for the runtime/SDK and the sources.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
cd "$here"

manifest=io.github.cdevidal.MorseLogger.yml
APP=io.github.cdevidal.MorseLogger

command -v flatpak >/dev/null 2>&1 || {
    echo "flatpak is not installed - install it from your distribution first" >&2
    exit 1
}

# flatpak-builder may be installed either on the host or as the Flathub Builder app
if command -v flatpak-builder >/dev/null 2>&1; then
    builder="flatpak-builder"
else
    builder="flatpak run --filesystem=$PWD org.flatpak.Builder"
fi

echo "== remotes and runtimes =="
flatpak remote-add --user --if-not-exists flathub \
    https://dl.flathub.org/repo/flathub.flatpakrepo 2>/dev/null || true
flatpak install --user -y --noninteractive \
    flathub org.flatpak.Builder org.freedesktop.Platform//24.08 org.freedesktop.Sdk//24.08

echo "== build =="
$builder --user --force-clean --disable-rofiles-fuse \
    --repo=repo build-dir "$manifest"

echo "== bundle =="
flatpak build-bundle repo morse-logger.flatpak "$APP"
ls -lh morse-logger.flatpak

if [ "${1:-}" != "--no-install" ]; then
    echo "== install for this user =="
    flatpak install --user -y --reinstall ./morse-logger.flatpak
    echo
    echo "run it with:  flatpak run $APP status"
fi
