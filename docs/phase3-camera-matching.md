# Phase 3 - Public camera to OSM road matching

Development branch: `phase3-camera-matching`.

## Purpose

Phase 3 converts the nationwide public unmanned traffic camera dataset into a Camera Ninja runtime camera DB linked to the compact OSM road graph.

The public standard dataset contains camera coordinates plus road metadata such as road type, route number/name, road direction, enforcement type, speed limit, location description and data date. Camera Ninja preserves these source fields and adds OSM segment matching fields.

## Input

- Compact road DB from Phase 2 (`schemaVersion=3`)
- Korea Public Data Portal nationwide unmanned traffic camera standard dataset

## Camera normalization

- `단속구분`:
  - `1`, `01` -> `SPEED`
  - `2`, `02` -> `SIGNAL`
  - `1+2`, `01+02` variants -> `SPEED_SIGNAL`
  - all others -> `OTHER`
- default Camera Ninja alerts: `SPEED`, `SPEED_SIGNAL`
- speed limit `0` -> `NULL` / unknown, never spoken as `0 km/h`
- road direction `01/1`, `02/2`, `03/3` is normalized to `1/2/3` while preserving the raw value.
- production camera identity uses a deterministic hash of the complete canonical public-data record so non-identical duplicate identifiers are never silently dropped.

## Road direction semantics

The public-data standard defines road route direction as:

- `01` / `1`: 상행 - road route endpoint -> starting point
- `02` / `2`: 하행 - road route starting point -> endpoint
- `03` / `3`: 양방향

This is **not a compass bearing**. The runtime must not convert `1/2/3` directly into north/east/south/west.

OSM way orientation is also not guaranteed to equal the official Korean road-route start/end orientation. Therefore Phase 3 stores the public direction code, OSM segment bearing, side of segment, `oneway`, road name/reference and location description separately.

### Conservative travel-direction derivation

`derive_camera_direction.py` adds four runtime fields:

- `direction_mode`
- `travel_sign`
- `direction_confidence`
- `direction_hard_filter`

`travel_sign` is relative to OSM segment orientation: `+1` means `from_node -> to_node`, `-1` the reverse, `0` both directions and `NULL` unresolved.

Evidence is deliberately tiered:

1. OSM one-way topology is strong evidence and may become a hard filter.
2. Public direction code `3` on a two-way OSM segment is treated as both directions.
3. On two-way roads, Camera Ninja can calibrate a local OSM way when both public direction codes (`1` and `2`) occur on opposite sides of the same way with sufficient match quality.
4. A camera's roadside position relative to a centerline is otherwise only a soft hint. It must not suppress a warning by itself.
5. Anything ambiguous remains unresolved and Android falls back to live GPS/heading/topology evidence.

This deliberately trades some extra warnings for avoiding false suppression of a real speed camera.

## Matching procedure

For each camera:

1. Query nearby compact OSM segments through `segment_rtree`.
2. Decode the candidate polyline geometry.
3. Project the camera coordinate onto each candidate polyline.
4. Rank candidates primarily by snap distance.
5. Increase confidence when public road name and/or route number agree with OSM metadata.
6. Penalize implausible road-class combinations such as a motorway camera matched to a residential road.
7. Store the selected segment and projection offset.
8. Preserve low-confidence/unmatched records instead of silently dropping them.

Default search radius is 120 m, expanded to 300 m when necessary. A candidate farther than 250 m is rejected.

## Runtime camera table

Important fields:

- `camera_id`
- source camera/provider identifiers
- source latitude/longitude
- `enforcement_class`
- `alert_default`
- `speed_limit`
- raw + normalized road direction
- `segment_id` (snapshot-local convenience ID)
- `segment_way_id`
- `segment_from_node`
- `segment_to_node`
- `segment_offset`
- `snap_distance_m`
- snapped coordinate
- `segment_bearing_deg`
- `side_of_segment`
- OSM road attributes
- `match_confidence`
- `match_status`
- candidate count/gap
- direction fields listed above

### Stable road reference warning

`segment_id` is compact-build-local and may change after OSM updates. Camera Ninja must not persist it across independent road DB versions as a universal identity. The DB also stores `way_id + from_node + to_node` as the more stable road reference, and road/camera DB versions must be treated as a matched pair.

## Confidence policy

`HIGH`, `MEDIUM`, `LOW`, `REVIEW` are diagnostic labels, not legal certainty.

- close snap + unambiguous candidate -> high
- road-name/route agreement raises confidence
- nearly tied adjacent candidates lower confidence
- long snap distances lower confidence
- unmatched/too-far records remain in the DB for fallback handling

Android V2 should use OSM road-based alerting when the vehicle road match and camera-road match are sufficiently confident. Otherwise it should fall back to the V1 GPS distance/heading/bearing approach.

For camera travel direction, Android may reject a camera solely on direction only when `direction_hard_filter=1` **and** the live vehicle road match is also high confidence. See `docs/android-integration-contract.md`.

## Bootstrap validation (2026-09 dataset used during development)

A full local dry-run was performed against 43,347 public records and the Phase 2 compact Korea road graph.

- <= 25 m snap: 39,397
- 25-50 m: 2,952
- 50-100 m: 838
- 100-150 m: 94
- 150-250 m: 40
- > 250 m: 12 (would be rejected by production max-snap policy)
- no candidate: 14

Distance percentiles before the 250 m production rejection:

- median: 7.39 m
- p90: 23.65 m
- p95: 34.72 m
- p99: 68.42 m

Additional exploratory direction analysis on the same snapshot found:

- public direction `1/2` records matched to a road: 24,987
- of those, 7,056 were on OSM one-way ways (strong topology evidence)
- 17,931 were on OSM two-way ways and therefore cannot safely be mapped from the public code to OSM orientation directly
- 2,147 OSM ways had usable observations of both public direction codes on opposite roadside positions
- 1,105 of those ways met the initial >=75% opposite-side consistency rule; 2,628 nearby code-1/2 camera observations lie on those locally calibratable ways

These figures are exploratory quality diagnostics, not a claim that every calibrated camera direction is correct. The production script therefore keeps weaker roadside inferences as soft scoring hints and reserves hard filtering for stronger evidence.

## Automation

`build-camera-db.yml` rebuilds the compact road graph, fetches the latest nationwide public camera data using the `DATA_GO_KR_SERVICE_KEY` GitHub Actions secret, performs matching, derives direction metadata, validates the output, compresses the camera DB, and stores a one-day test artifact.

Large runtime binaries are intentionally not committed to Git history.
