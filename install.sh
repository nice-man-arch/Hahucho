#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ID="io.github.nice-man-arch.hakucho"
DEST="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/plugins/$ID"
command -v python3 >/dev/null || { echo 'Install Python 3 with: sudo pacman -S python' >&2; exit 1; }
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' || { echo 'Python 3.12 or newer is required.' >&2; exit 1; }
command -v curl >/dev/null || { echo 'Install curl with: sudo pacman -S curl' >&2; exit 1; }
command -v mpv >/dev/null || { echo 'Install mpv with: sudo pacman -S mpv' >&2; exit 1; }
command -v ffmpeg >/dev/null || { echo 'Install ffmpeg with: sudo pacman -S ffmpeg' >&2; exit 1; }
command -v omarchy >/dev/null || { echo 'Run this installer inside Omarchy (omarchy command not found).' >&2; exit 1; }
command -v omarchy-shell >/dev/null || { echo 'omarchy-shell is required to register the Quickshell widget.' >&2; exit 1; }
if [[ -e "$DEST" ]]; then
  echo "Refusing to overwrite existing plugin directory: $DEST" >&2
  echo 'Move it aside or remove it yourself, then run this installer again.' >&2
  exit 1
fi
mkdir -p "$(dirname "$DEST")"
cp -a "$ROOT" "$DEST"
rm -rf "$DEST/.git"
omarchy-shell shell rescanPlugins
omarchy plugin enable "$ID"
echo "Hakuchō installed at $DEST. The backend starts when the Quickshell widget loads."
