#!/usr/bin/env bash
# Start/check the self-contained local JSON backend. No Python packages are needed.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
say() { printf '[hakucho] %s\n' "$*"; }
command -v python3 >/dev/null || { echo 'Python 3 is required (Arch package: python)' >&2; exit 1; }
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' || { echo 'Python 3.12 or newer is required.' >&2; exit 1; }
command -v curl >/dev/null || { echo 'curl is required (Arch package: curl)' >&2; exit 1; }
command -v mpv >/dev/null || say 'mpv is not installed yet; install it with: sudo pacman -S mpv'
command -v ffmpeg >/dev/null || say 'Downloads need ffmpeg; install it with: sudo pacman -S ffmpeg'
if curl -fsS --max-time 1 http://127.0.0.1:8765/health 2>/dev/null | python3 -c 'import json,sys; raise SystemExit(0 if json.load(sys.stdin).get("download_manager",0) >= 4 else 1)' 2>/dev/null; then
  say 'backend already running'
else
  # A pre-download backend may already own the port. Restart only a process
  # whose argv contains this plugin's exact backend file path.
  stale_pid="$(python3 - "$DIR/backend/server.py" <<'PY'
import os, pathlib, sys
target = sys.argv[1]
for entry in pathlib.Path('/proc').glob('[0-9]*'):
    try:
        args = (entry / 'cmdline').read_bytes().decode(errors='ignore').split('\0')
        if target in args and int(entry.name) != os.getpid():
            print(entry.name)
            break
    except (OSError, ValueError):
        pass
PY
  )"
  if [[ -n "$stale_pid" ]]; then
    kill "$stale_pid" 2>/dev/null || true
    for _ in {1..20}; do
      kill -0 "$stale_pid" 2>/dev/null || break
      sleep 0.1
    done
  fi
  runtime="${XDG_STATE_HOME:-$HOME/.local/state}/hakucho"
  mkdir -p "$runtime"
  setsid python3 "$DIR/backend/server.py" >>"$runtime/backend.log" 2>&1 </dev/null &
  for _ in {1..30}; do
    if curl -fsS --max-time 1 http://127.0.0.1:8765/health >/dev/null 2>&1; then say 'backend ready'; exit 0; fi
    sleep 0.2
  done
  echo "Backend did not start; inspect $runtime/backend.log" >&2
  exit 1
fi
