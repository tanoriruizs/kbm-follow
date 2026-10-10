#!/usr/bin/env bash
# Detiene y quita el LaunchAgent. Tu configuración y el registro se quedan, por si lo reinstalas.
set -euo pipefail

LABEL="com.kbmfollow.agent"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$PLIST"
echo "kbm-follow desinstalado."
