#!/usr/bin/env bash
# Instala kbm-follow como LaunchAgent: arranca solo cada vez que inicias sesión en la Mac.
set -euo pipefail

LABEL="com.kbmfollow.agent"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VENV="$ROOT/.venv"
PYTHON="$VENV/bin/python"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LAUNCHD_LOG="$HOME/Library/Logs/kbm-follow.launchd.log"
DOMAIN="gui/$(id -u)"

# macOS no deja que los procesos en segundo plano lean Documentos, Escritorio ni
# Descargas. Desde ahí el agente arrancaría y moriría sin decir mucho.
case "$ROOT" in
  "$HOME/Documents"*|"$HOME/Desktop"*|"$HOME/Downloads"*)
    echo "ERROR: mueve el proyecto fuera de Documentos/Escritorio/Descargas, por ejemplo:" >&2
    echo "  mv \"$ROOT\" ~/kbm-follow && ~/kbm-follow/install/macos/install.sh" >&2
    exit 1 ;;
esac

if [[ ! -x "$PYTHON" ]]; then
  echo "-> Creando el entorno virtual..."
  python3 -m venv "$VENV"
fi

echo "-> Instalando kbm-follow..."
"$PYTHON" -m pip install --quiet --disable-pip-version-check -e "$ROOT"

if ! (cd "$ROOT" && "$PYTHON" -m kbm_follow status); then
  echo "Primero crea la configuración, por ejemplo:" >&2
  echo "  .venv/bin/kbm-follow init --channel 2 --leader \"F75\" --follower \"logitech-hidpp:MX Master 3S\" --secret <SECRETO>" >&2
  exit 1
fi

# Las rutas van dentro de un XML: si tuvieran &, < o > el plist quedaría roto.
xml_escape() { sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' <<<"$1"; }

echo "-> Registrando el LaunchAgent..."
mkdir -p "$(dirname "$PLIST")" "$(dirname "$LAUNCHD_LOG")"
# KeepAlive lo revive si se cae; ProcessType Interactive evita que macOS lo frene
# para ahorrar batería (queremos que reaccione al instante).
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$(xml_escape "$PYTHON")</string>
    <string>-m</string>
    <string>kbm_follow</string>
    <string>run</string>
  </array>
  <key>WorkingDirectory</key><string>$(xml_escape "$ROOT")</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$(xml_escape "$LAUNCHD_LOG")</string>
  <key>StandardErrorPath</key><string>$(xml_escape "$LAUNCHD_LOG")</string>
</dict>
</plist>
EOF

# Si ya estaba instalado, lo descargamos primero para que tome la versión nueva.
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"

echo
echo "Listo. kbm-follow ya corre en segundo plano y arrancará solo al iniciar sesión."
echo "Registro: $HOME/Library/Logs/kbm-follow.log"
echo "Si macOS pregunta si Python puede buscar dispositivos en la red local, elige 'Permitir'."
