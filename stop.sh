#!/bin/sh
# Stop the background codex-autoresume agent and remove it from launchd.
cd "$(dirname "$0")"
python3 -m codex_autoresume uninstall-launchd
