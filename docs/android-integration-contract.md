# Camera Ninja Android integration contract

This document defines the runtime contract between the prebuilt nationwide road/camera data and the Android app. It is deliberately conservative: road-based direction filtering is used only when the data says it is safe; otherwise the existing V1 GPS heading/bearing logic remains the fallback.

## Runtime files

Android keeps two local SQLite databases as a matched version pair:

- `camera-ninja-korea-roads-compact.db`
- `camera-ninja-korea-cameras.db`

The download form may be Zstandard-compressed. The app verifies SHA-256 before replacing an installed database and must never replace the working pair partially.

## Road DB

Important tables:

- `ways`
- `segments`
- `segment_rtree`
- `metadata`

A road segment is a junction-split polyline. `segment_id` is snapshot-local and may change after an OSM rebuild. The app must treat road DB and camera DB versions as a pair.

### Geometry BLOB

Encoding is `1e-7 degree integer -> delta -> zigzag -> varint`.

Decoder outline:

1. Read unsigned varint point count.
2. For each point read two unsigned varints: delta-lat and delta-lon.
3. Zigzag-decode each delta.
4. Accumulate integer latitude/longitude.
5. Divide by `10_000_000.0`.

## Camera DB

Important fields:

- `camera_id`
- `latitude`, `longitude`
- `enforcement_class`
- `alert_default`
- `speed_limit`
- `segment_id`
- `segment_way_id`
- `segment_from_node`, `segment_to_node`
- `segment_offset`
- `snap_distance_m`
- `match_confidence`, `match_status`
- `road_direction_raw`, `road_direction_norm`
- `direction_mode`
- `travel_sign`
- `direction_confidence`
- `direction_hard_filter`

`travel_sign` is relative to the OSM segment orientation:

- `+1`: OSM `from_node -> to_node`
- `-1`: reverse direction
- `0`: both directions
- `NULL`: unresolved

## Hard direction filtering

The Android runtime may reject a camera solely on direction only when `direction_hard_filter = 1` and the vehicle road match is itself high confidence.

Recommended rule:

```text
if roadMatch.confidence >= 0.80 and camera.direction_hard_filter == 1:
    require vehicleTravelSign == camera.travel_sign
else:
    do not reject solely because of camera travel_sign
```

`ROADSIDE_HINT`, unresolved two-way modes, and low-confidence calibration are scoring features only. They must not suppress a warning by themselves.

## Vehicle road matching

For each valid location sample:

1. Query `segment_rtree` around the current GPS point (initial radius about 50-80 m, expand when needed).
2. Decode candidate polylines.
3. Project GPS onto each candidate polyline.
4. Score by perpendicular distance, heading agreement, previous-segment continuity, road class, bridge/tunnel/layer continuity and OSM one-way legality.
5. Keep the previous road match unless a new candidate wins by a meaningful margin. This prevents parallel-road flapping.
6. Produce:
   - `segmentId`
   - `wayId`
   - `offset` (0..1)
   - `travelSign` (+1/-1)
   - `confidence` (0..1)

Suggested high-confidence conditions include GPS accuracy <= 30 m, actual movement, consistent heading and stable segment continuity.

## Cameras ahead

Do not use straight-line distance alone once road matching is high confidence.

Starting from the current matched segment and offset:

1. Traverse the road graph in the vehicle travel direction.
2. Accumulate road distance up to the configured look-ahead range (for example 2 km).
3. Query cameras attached to the traversed segments.
4. For the current segment, compare camera offset with current vehicle offset according to travel sign.
5. Apply hard camera direction filtering only under the rule above.
6. Rank the remaining cameras by graph distance ahead.

Turn restrictions are not yet encoded in the Phase 2 road DB. Until they are added, graph traversal should be conservative at ambiguous junctions. Prefer continuity of the current road name/ref, heading and road class, and avoid treating every connected branch as equally likely.

## Alert trigger

Recommended road-based alert eligibility:

```text
roadMatch.confidence >= threshold
camera.match_confidence >= threshold
camera is reachable ahead on graph
camera graphDistance crossed warningDistance threshold
camera not already alerted in this driving session
```

Default alert classes remain `SPEED` and `SPEED_SIGNAL`. Speed limit `NULL` means unknown and must never be spoken as zero.

## Fallback

Fallback to the existing V1 engine when any of the following applies:

- no nearby OSM segment
- GPS accuracy is poor
- vehicle road-match confidence is low
- new/unmapped road
- camera road match is low/review/unmatched
- topology ahead is ambiguous enough that the road result cannot be trusted

V1 fallback retains smoothed GPS heading, bearing-to-camera, approaching-distance trend, lateral plausibility and one-alert-per-session behaviour.

## Bluetooth lifecycle

Do not change the proven V1 lifecycle:

- registered Tesla/car Bluetooth connection starts monitoring
- disconnect keeps the existing debounce/recheck behaviour
- disconnect stops location updates, road matching and TTS
- foreground-service notification exists only while monitoring

## Atomic updates

Road and camera databases are coupled by the road snapshot. A safe update is:

1. Download manifest.
2. Download both compressed DBs for the same data version.
3. Verify SHA-256.
4. Decompress to temporary filenames.
5. Validate SQLite integrity and schema versions.
6. Close current DB handles.
7. Atomically swap both files.
8. Reopen and run a small R-tree/camera sanity query.
9. Delete old files only after successful reopen.

Never update only the camera DB if it was built against a different road snapshot.

## Codex implementation boundary

V2 should extend the existing Android app rather than replace it. The new modules should be separable, for example:

- `RoadDatabase`
- `RoadGeometryDecoder`
- `RoadMatcher`
- `RoadGraphNavigator`
- `RoadCameraRepository`
- `RoadCameraAlertEngine`
- `RuntimeDataUpdater`

Existing Bluetooth lifecycle, foreground service, location acquisition, TTS/audio ducking, settings and GPS fallback stay in place.
