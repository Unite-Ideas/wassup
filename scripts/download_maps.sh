#!/usr/bin/env bash
# Downloads the maps for the MAP view into data/maps. Run from the wassup folder:
#
#   bash scripts/download_maps.sh              world map plus every region below
#   bash scripts/download_maps.sh ukraine      just one region (or several, space separated)
#   bash scripts/download_maps.sh world        just the world map
#
# Maps are OpenStreetMap data from Protomaps' daily planet build. Only the parts you ask for are
# downloaded (the full planet is 138 GB). Uses Docker to run the pmtiles tool, so nothing else
# needs installing. Safe to run again later to refresh the maps.
set -euo pipefail

# name   bounding box (west,south,east,north)   deepest zoom
REGIONS=(
  "ukraine   22.0,44.0,40.5,52.6    14"   # Ukraine, Crimea, Donbas and the Russian border
  "mideast   32.0,12.0,63.5,42.0    14"   # Israel, Gaza, Lebanon, Syria, Iraq, Iran, Yemen, the Gulf
  "mexico   -118.5,7.0,-77.0,33.0   14"   # Mexico and Central America, up to the US border
)
WORLD_ZOOM=9   # city level everywhere

cd "$(dirname "$0")/.."
mkdir -p data/maps

BUILD=$(curl -fsSL https://build-metadata.protomaps.dev/builds.json | python3 -c 'import json,sys; print(json.load(sys.stdin)[-1]["key"])')
SRC="https://build.protomaps.com/${BUILD}"
echo "Using Protomaps build ${BUILD}"

extract() {  # name, extra pmtiles arguments...
  local name=$1; shift
  echo
  echo "== ${name} =="
  mkdir -p data/maps/partial
  docker run --rm -v "$PWD/data/maps:/data" protomaps/go-pmtiles:latest \
    extract "$SRC" "/data/partial/${name}.pmtiles" "$@" --download-threads=8
  mv -f "data/maps/partial/${name}.pmtiles" "data/maps/${name}.pmtiles"
  echo "${name}: $(du -h "data/maps/${name}.pmtiles" | cut -f1)"
}

want=("$@")
[ ${#want[@]} -eq 0 ] && want=(world ukraine mideast mexico)

for w in "${want[@]}"; do
  if [ "$w" = world ]; then
    extract world --maxzoom=$WORLD_ZOOM
    continue
  fi
  found=0
  for r in "${REGIONS[@]}"; do
    read -r name bbox zoom <<<"$r"
    if [ "$name" = "$w" ]; then extract "$name" --bbox="$bbox" --maxzoom="$zoom"; found=1; fi
  done
  [ $found = 1 ] || echo "Unknown region '$w'. Known: world $(for r in "${REGIONS[@]}"; do echo -n "${r%% *} "; done)"
done

echo
echo "Done. Restart Wassup to load the maps:  docker compose up -d app"
