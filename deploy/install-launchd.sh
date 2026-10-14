#!/bin/bash
# 把减重计划注册为当前用户的 LaunchAgent：登录后自动启动，进程退出后自动重启。
# 用法：deploy/install-launchd.sh            （默认端口 8765）
#       PORT=9000 deploy/install-launchd.sh
# 卸载：launchctl bootout gui/$(id -u)/com.weightplanner && rm ~/Library/LaunchAgents/com.weightplanner.plist
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
UV="$(command -v uv || true)"
if [ -z "$UV" ]; then
  echo "找不到 uv，请先安装：brew install uv" >&2
  exit 1
fi
PORT="${PORT:-8765}"
LABEL=com.weightplanner
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$DIR/data" "$HOME/Library/LaunchAgents"
(cd "$DIR" && "$UV" sync --no-dev)
sed -e "s#__UV__#$UV#g" -e "s#__DIR__#$DIR#g" -e "s#__PORT__#$PORT#g" \
  "$DIR/deploy/$LABEL.plist" > "$PLIST"
plutil -lint "$PLIST" >/dev/null

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "已启动，日志在 $DIR/data/server.log"
echo "本机访问：http://localhost:$PORT"
echo "手机访问：http://$(scutil --get LocalHostName).local:$PORT"
