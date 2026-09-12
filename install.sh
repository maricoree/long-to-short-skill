#!/usr/bin/env bash
# Install long-to-short as a Claude Code skill.
#
# Copies this directory to ~/.claude/skills/long-to-short, which is where
# Claude Code looks for personal skills. Override with SKILLS_DIR.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dest="${SKILLS_DIR:-$HOME/.claude/skills}/long-to-short"

if [ "$here" = "$dest" ]; then
  echo "already installed at $dest"
  exit 0
fi

if [ -e "$dest" ]; then
  read -r -p "$dest exists. Overwrite? [y/N] " reply
  case "$reply" in [yY]*) rm -rf "$dest" ;; *) echo "aborted"; exit 1 ;; esac
fi

mkdir -p "$(dirname "$dest")"
cp -R "$here" "$dest"
rm -rf "$dest/.git" "$dest/__pycache__" "$dest/tools/__pycache__"

echo "installed -> $dest"
echo
echo "Check your dependencies:"
command -v ffmpeg  >/dev/null && echo "  ffmpeg   ok" || echo "  ffmpeg   MISSING -- put it on PATH"
command -v ffprobe >/dev/null && echo "  ffprobe  ok" || echo "  ffprobe  MISSING -- put it on PATH"
if python -c "import numpy, PIL" 2>/dev/null; then
  echo "  numpy+Pillow  ok"
else
  echo "  numpy+Pillow  MISSING -- pip install -r requirements.txt"
fi

if python -c "import faster_whisper" 2>/dev/null; then
  echo "  faster-whisper  ok"
else
  echo "  faster-whisper  absent -- optional, and only needed for a video that"
  echo "                  has no subtitles: pip install faster-whisper"
fi
