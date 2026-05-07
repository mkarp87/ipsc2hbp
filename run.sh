#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [ -x "venv/bin/python" ]; then
  exec venv/bin/python ipsc2hbp.py -c ipsc2hbp.toml "$@"
fi
exec python3 ipsc2hbp.py -c ipsc2hbp.toml "$@"
