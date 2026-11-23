#!/usr/bin/env bash
set -euo pipefail

echo "== Installing Python dependencies =="
pip install -r requirements.txt

echo "== Installing static FFmpeg =="
mkdir -p /opt/render/project/src/bin
if ! command -v ffmpeg >/dev/null 2>&1; then
  # static binary for Linux x86_64 (johnvansickle.com)
  curl -L --retry 3 -o ffmpeg.tar.xz \
    https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz
  tar -xJf ffmpeg.tar.xz
  FFMPEG_DIR=$(find . -maxdepth 1 -type d -name 'ffmpeg-*static*' | head -n1)
  cp "$FFMPEG_DIR/ffmpeg" /opt/render/project/src/bin/ffmpeg
  chmod +x /opt/render/project/src/bin/ffmpeg
  rm -rf ffmpeg.tar.xz "$FFMPEG_DIR"
fi

echo "FFmpeg: $(/opt/render/project/src/bin/ffmpeg -version | head -n1)"
