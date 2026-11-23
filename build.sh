#!/usr/bin/env bash
set -uo pipefail

echo "== Installing Python dependencies =="
pip install -r requirements.txt

echo "== Installing FFmpeg =="
mkdir -p /opt/render/project/src/bin

# Tenta binário estático (rápido); se falhar, usa apt-get.
if ! command -v ffmpeg >/dev/null 2>&1 && [ ! -x /opt/render/project/src/bin/ffmpeg ]; then
  if curl -fL --retry 3 --max-time 120 -o ffmpeg.tar.xz \
      https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz; then
    tar -xJf ffmpeg.tar.xz
    FFMPEG_DIR=$(find . -maxdepth 1 -type d -name 'ffmpeg-*static*' | head -n1)
    if [ -n "$FFMPEG_DIR" ]; then
      cp "$FFMPEG_DIR/ffmpeg" /opt/render/project/src/bin/ffmpeg
      chmod +x /opt/render/project/src/bin/ffmpeg
    fi
    rm -rf ffmpeg.tar.xz "$FFMPEG_DIR"
  else
    echo "Static download failed, trying apt-get..."
    sudo apt-get update -qq && sudo apt-get install -y -qq ffmpeg || true
    command -v ffmpeg >/dev/null 2>&1 && cp "$(command -v ffmpeg)" /opt/render/project/src/bin/ffmpeg
  fi
fi

if [ -x /opt/render/project/src/bin/ffmpeg ]; then
  echo "FFmpeg: $(/opt/render/project/src/bin/ffmpeg -version 2>/dev/null | head -n1)"
else
  echo "WARNING: FFmpeg not installed; video merge/conversion will be unavailable."
fi

echo "== Build finished =="
