#!/bin/sh
set -eu
# The shared release workflow requires this entrypoint and reads notes on stdout.
exec python3 tools/prepare_release.py "$@"
