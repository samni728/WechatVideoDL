#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

echo "[1/4] Python"
python3 --version

echo "[2/4] OpenCLI"
if command -v opencli >/dev/null 2>&1; then
  opencli --version || true
  opencli doctor || true
else
  echo "ERROR: 没有检测到 opencli。请先安装 OpenCLI，并确认 opencli doctor 正常。" >&2
  exit 2
fi

echo "[3/4] yt-dlp"
if command -v yt-dlp >/dev/null 2>&1; then
  yt-dlp --version
else
  echo "系统没有 yt-dlp，安装项目本地版本到 .venv ..."
  python3 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip yt-dlp
  .venv/bin/python -m yt_dlp --version
fi

echo "[4/4] ffmpeg / ffprobe"
command -v ffmpeg || echo "WARN: ffmpeg 未安装；当前直链 MP4 下载仍可工作，但部分媒体处理能力会受限。"
command -v ffprobe || echo "WARN: ffprobe 未安装；下载后将跳过媒体信息验证。"

echo
echo "依赖检查完成。"
python3 app.py --check
