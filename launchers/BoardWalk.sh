#!/bin/sh
# Linux/Unix terminal launcher.
cd "$(dirname "$0")/.." || exit 1
exec python3 launchers/boardwalk_launcher.py
