#!/bin/zsh
# Daily JobHub run. Installed via scripts/install_launchd.sh; logs to data/logs/.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$ROOT/data/logs"
# launchd has a minimal PATH; make sure `claude` (headless Claude Code) is reachable.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
cd "$ROOT"
exec "$ROOT/.venv/bin/jobhub" run >> "$ROOT/data/logs/$(date +%F).log" 2>&1
