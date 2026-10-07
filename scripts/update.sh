#!/usr/bin/env bash
# Get the latest Wassup code and restart with it:   bash ~/wassup/scripts/update.sh
set -uo pipefail
cd "$(dirname "$0")/.."
echo "Getting the latest code..."
git pull || { echo "  [!!]  git pull failed (see above). Nothing was changed."; exit 1; }
BUILD=1 bash scripts/start.sh
