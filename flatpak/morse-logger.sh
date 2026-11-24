#!/bin/sh
# Launch morse-logger inside the Flatpak sandbox.
#
# The data directory defaults to ~/morse-logger, which --persist=morse-logger
# maps to ~/.var/app/io.github.cdevidal.MorseLogger/morse-logger on the host.
export PYTHONPATH="/app/lib/morse-logger${PYTHONPATH:+:$PYTHONPATH}"
export MORSE_LOGGER_HOME="${MORSE_LOGGER_HOME:-$HOME/morse-logger}"
exec python3 -m morse_logger.cli "$@"
