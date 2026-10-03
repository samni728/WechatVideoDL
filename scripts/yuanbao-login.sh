#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

SESSION="wxydl_yuanbao_login"

if ! command -v opencli >/dev/null 2>&1; then
  echo "ERROR: 找不到 opencli。" >&2
  exit 2
fi

echo "正在前台打开元宝登录页..."
opencli browser "$SESSION" open 'https://yuanbao.tencent.com/' --window foreground >/dev/null
opencli browser "$SESSION" wait time 1 >/dev/null || true

# 如果当前仍是未登录状态，打开登录二维码。
opencli browser "$SESSION" click --role button --name '登录' >/dev/null 2>&1 || true

echo
echo "请在刚刚打开的元宝页面完成微信扫码登录。"
echo "完成后回到这里按 Enter，我会检查登录状态。"
read -r

STATE="$(opencli browser "$SESSION" state 2>&1 || true)"
if printf '%s' "$STATE" | grep -q '未登录'; then
  echo "WARN: 页面仍显示“未登录”。请确认扫码授权已经完成。" >&2
  echo "我会保留这个页面，方便你继续操作。"
  exit 1
fi

echo "元宝页面已不再显示“未登录”。登录 Cookie 会保留在当前 Chrome Profile 中。"
opencli browser "$SESSION" close >/dev/null 2>&1 || true
