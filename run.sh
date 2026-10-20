#!/bin/sh
# BoardWalk release launcher. Loopback only.
set -eu
cd "$(dirname "$0")"
export KHAOSS_PORT="${KHAOSS_PORT:-7842}"
exec python3 panel_server.py --port "$KHAOSS_PORT" --foreground
