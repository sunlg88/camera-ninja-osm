#!/usr/bin/env bash
set -euo pipefail

SOURCE_URL="${SOURCE_URL:-https://download.geofabrik.de/asia/south-korea-latest.osm.pbf}"
MD5_URL="${MD5_URL:-https://download.geofabrik.de/asia/south-korea-latest.osm.pbf.md5}"
WORK_DIR="${WORK_DIR:-work}"
OUT_DIR="${OUT_DIR:-dist}"
FILTER_FILE="${FILTER_FILE:-config/osm_filter.txt}"

mkdir -p "$WORK_DIR" "$OUT_DIR"

SOURCE_PBF="$WORK_DIR/south-korea-latest.osm.pbf"
SOURCE_MD5="$WORK_DIR/south-korea-latest.osm.pbf.md5"
ROADS_PBF="$OUT_DIR/south-korea-roads.osm.pbf"
MANIFEST="$OUT_DIR/manifest.json"

printf 'Downloading %s\n' "$SOURCE_URL"
curl -fL --retry 3 --retry-delay 5 "$SOURCE_URL" -o "$SOURCE_PBF"
curl -fL --retry 3 --retry-delay 5 "$MD5_URL" -o "$SOURCE_MD5"

# Geofabrik checksum files contain the source basename. Verify from the work dir.
(
  cd "$WORK_DIR"
  md5sum -c "$(basename "$SOURCE_MD5")"
)

printf 'Extracting all highway=* ways and supporting road relations...\n'
osmium tags-filter \
  --expressions="$FILTER_FILE" \
  --overwrite \
  -o "$ROADS_PBF" \
  "$SOURCE_PBF"

printf 'Checking referenced OSM objects...\n'
osmium check-refs "$ROADS_PBF"

SOURCE_SHA256="$(sha256sum "$SOURCE_PBF" | awk '{print $1}')"
ROADS_SHA256="$(sha256sum "$ROADS_PBF" | awk '{print $1}')"
SOURCE_BYTES="$(stat -c '%s' "$SOURCE_PBF")"
ROADS_BYTES="$(stat -c '%s' "$ROADS_PBF")"
GENERATED_AT="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
OSM_HEADER="$(osmium fileinfo -e -g header.option.osmosis_replication_timestamp "$SOURCE_PBF" 2>/dev/null || true)"

python3 - "$MANIFEST" <<PY
import json
import os
import sys

manifest_path = sys.argv[1]
data = {
    "schemaVersion": 1,
    "region": "south-korea",
    "source": "Geofabrik / OpenStreetMap",
    "sourceUrl": os.environ.get("SOURCE_URL", "https://download.geofabrik.de/asia/south-korea-latest.osm.pbf"),
    "generatedAt": "${GENERATED_AT}",
    "osmReplicationTimestamp": "${OSM_HEADER}",
    "sourceBytes": int("${SOURCE_BYTES}"),
    "roadsBytes": int("${ROADS_BYTES}"),
    "sourceSha256": "${SOURCE_SHA256}",
    "roadsSha256": "${ROADS_SHA256}",
    "roadFilter": "all OSM highway=* ways plus road restriction/route relations and referenced objects",
    "artifact": "south-korea-roads.osm.pbf"
}
with open(manifest_path, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
    f.write("\n")
PY

printf '\nBuild complete\n'
printf 'Source: %s bytes\n' "$SOURCE_BYTES"
printf 'Roads : %s bytes\n' "$ROADS_BYTES"
printf 'Manifest: %s\n' "$MANIFEST"
