# Flatpak

A sandboxed build of morse-logger: same program, packaged with its decoder so a
machine only needs Flatpak, not the offline `.deb` bundle.

**Status: built and exercised on the development machine, but not yet on a clean
second distribution.** The manifest below compiles fltk, pulseaudio's `pactl`,
fldigi, rtl-sdr and the app itself; while building in a new environment expect to
fix a dependency or a configure flag.

## Build

    cd flatpak
    ./build.sh                 # builds, bundles and installs for your user
    ./build.sh --no-install    # only build and bundle

`build.sh` adds the Flathub remote if needed and installs `org.flatpak.Builder`,
`org.freedesktop.Platform//24.08` and `org.freedesktop.Sdk//24.08` (~1.5 GB, once).
The result is `flatpak/morse-logger.flatpak`.

    flatpak run io.github.cdevidal.MorseLogger status
    flatpak run io.github.cdevidal.MorseLogger start --dial 7.039

## Where things live

| what | inside the sandbox | on the host |
|---|---|---|
| data, logs, config | `~/morse-logger` | `~/.var/app/io.github.cdevidal.MorseLogger/morse-logger` |
| sound | the host's PulseAudio/PipeWire socket (`--socket=pulseaudio`) | unchanged |
| SDR | the dongle, via `--device=all` | no udev rules needed |

`--persist=morse-logger` is what makes `~/morse-logger` survive updates and be
visible on the host, so you can keep editing `config.json` outside the sandbox.

## Headless machines

The sandbox has **no X server of its own** and the freedesktop runtime does not
ship `Xvfb`, so on a machine without a desktop run it under the host's `xvfb-run`:

    # Debian/Ubuntu: apt install xvfb
    xvfb-run -a flatpak run io.github.cdevidal.MorseLogger start --dial 7.039

On a desktop distribution the session display is used and no extra step is
needed. Building `xorg-server` into the manifest would remove this caveat; it is
not done yet because of the size of that dependency chain.

## Offline machines and USB sticks

Yes — two ways, both work with no network on the destination.

**One file (simplest).** `build.sh` already produces `morse-logger.flatpak`:

    flatpak install --user ./morse-logger.flatpak

The app bundle does **not** include the runtime, so also bundle that once:

    flatpak build-bundle /var/lib/flatpak/repo platform.flatpak \
        org.freedesktop.Platform x86_64 24.08
    flatpak install --user ./platform.flatpak

**A USB repository (handles app + runtime together).**

    ./make-usb.sh /media/you/USB

That uses `flatpak create-usb`, which copies the app and its runtime into an
OSTree repo on the stick. On the offline machine:

    flatpak install --user --sideload-repo=/media/you/USB/.ostree/repo \
        io.github.cdevidal.MorseLogger

## Installing Flatpak itself offline

Flatpak is the one thing that must come from the destination's own packages.
On a machine with the same distribution release and network:

    # Debian / Ubuntu
    apt-get install --download-only -y flatpak
    cp /var/cache/apt/archives/*.deb /media/usb/debs/

    # Fedora
    dnf download --resolve --destdir=/media/usb/rpms flatpak

Then on the offline machine:

    sudo dpkg -i /media/usb/debs/*.deb        # Ubuntu/Debian
    sudo dnf install /media/usb/rpms/*.rpm    # Fedora

The packages must match the destination's release and architecture; that is why
the download happens on a machine of the same version rather than being shipped
with this repository.

## Known gaps

- `pactl` is built from source because the freedesktop runtime ships `libpulse`
  but not the client tools, and the cable management depends on it. That module
  is the most likely to need adjustment on a new SDK.
- fldigi may want `libsamplerate`, `portaudio` or `hamlib` depending on what its
  `configure` finds; the manifest builds it with defaults only.
- The app id `io.github.cdevidal.MorseLogger` assumes that GitHub account; change
  it here and in `build.sh`/`make-usb.sh` if yours differs.
- Publishing to Flathub would need the sources vendored with all their
  dependencies and built on Flathub's runners; this manifest is for personal or
  direct distribution.
