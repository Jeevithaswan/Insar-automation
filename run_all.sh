#!/bin/bash
# Runs the whole pipeline in one command. Stops only once, for the one
# genuine human decision the pipeline requires (picking a reference point
# from stage 6's candidate list) -- everything else runs unattended.
#
# Usage: ./run_all.sh [config_yaml]
# Default config: configs/runs/dummy_test_aoi.yaml
set -e

CONFIG=${1:-configs/runs/dummy_test_aoi.yaml}

echo "=== tracks ==="
docker compose run --rm insar tracks "$CONFIG"

for stage in 0 1 2 3 4 5 6; do
  echo
  echo "=== stage $stage ==="
  docker compose run --rm insar "$stage" "$CONFIG"
done

# run_id lives in the config yaml; candidates were saved by stage 6 to
# runs/<run_id>/review/candidate_reference_points.json (top-ranked first)
RUN_ID=$(grep -m1 '^run_id:' "$CONFIG" | sed 's/run_id: *//')
CANDIDATES_FILE="runs/${RUN_ID}/review/candidate_reference_points.json"
TOP_LAT=""
TOP_LON=""
if [ -f "$CANDIDATES_FILE" ]; then
  read -r TOP_LAT TOP_LON <<< "$(python3 -c "
import json
c = json.load(open('$CANDIDATES_FILE'))
print(c[0]['lat'], c[0]['lon']) if c else print('', '')
")"
fi

echo
echo "---------------------------------------------------------------"
echo "Reference point candidates are listed above (highest ranked first)."
if [ -n "$TOP_LAT" ]; then
  echo "Top candidate: lat=$TOP_LAT lon=$TOP_LON"
  echo "Press Enter to accept it, or type different lat/lon to override."
  read -rp "lat [$TOP_LAT]: " LAT
  read -rp "lon [$TOP_LON]: " LON
  LAT=${LAT:-$TOP_LAT}
  LON=${LON:-$TOP_LON}
else
  echo "Pick one and enter its lat and lon below."
  read -rp "lat: " LAT
  read -rp "lon: " LON
fi

echo
echo "=== stage 7 (reference point: $LAT, $LON) ==="
docker compose run --rm insar 7 "$LAT" "$LON" "$CONFIG"

echo
echo "=== stage 8 ==="
docker compose run --rm insar 8 "$CONFIG"

echo
echo "Done. Final outputs are in runs/<run_id>/deliverables/"
