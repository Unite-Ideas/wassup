#!/usr/bin/env bash
# Put "Start Wassup" and "Stop Wassup" on your Windows desktop:   bash ~/wassup/scripts/desktop-shortcuts.sh
# Double click them to start or stop Wassup without opening Ubuntu.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
# Ask Windows where the desktop is (it may be inside OneDrive).
win_desktop="$(powershell.exe -NoProfile -Command "[Environment]::GetFolderPath('Desktop')" | tr -d '\r')"
desktop="$(wslpath "$win_desktop")"
cp "$here/windows/Start Wassup.bat" "$here/windows/Stop Wassup.bat" "$desktop/"
echo "  [ok]  Put 'Start Wassup' and 'Stop Wassup' on your desktop ($win_desktop)."
