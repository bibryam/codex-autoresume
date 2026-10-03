#!/bin/sh
# Start codex-autoresume in the background (launchd agent, starts at every login) and open the page.
set -e
cd "$(dirname "$0")"
python3 -m codex_autoresume install-launchd
sleep 2
port=$(python3 -c 'from codex_autoresume.config import Config; print(Config.load().port)')
open "http://127.0.0.1:$port" 2>/dev/null || true
echo "running in the background; log: ~/.codex-autoresume/daemon.log"
