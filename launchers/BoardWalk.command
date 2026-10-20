#!/bin/bash
# macOS: double-click this file to start BoardWalk in the browser.
cd "$(dirname "$0")/.." || exit 1
exec python3 launchers/boardwalk_launcher.py
