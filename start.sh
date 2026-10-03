#!/bin/sh
# Start codex-resume in the background (launchd agent, survives logout/login) and open the page.
set -e
cd "$(dirname "$0")"
python3 -m codex_resume install-launchd
sleep 2
open "http://127.0.0.1:${CODEX_RESUME_PORT:-8765}" 2>/dev/null || true
echo "running in the background; log: ~/.codex-resume/daemon.log"
