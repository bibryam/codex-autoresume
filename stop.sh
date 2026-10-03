#!/bin/sh
# Stop the background codex-resume agent and remove it from launchd.
cd "$(dirname "$0")"
python3 -m codex_resume uninstall-launchd
