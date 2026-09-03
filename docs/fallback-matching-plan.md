# Camera Ninja fallback matching plan

Status: design contract for Phase 3 completion and Android V2 integration.

## Goal

Camera Ninja must remain useful when the OSM road match is ambiguous, temporarily wrong, or unavailable because of new roads / stale map data. The fallback must be conservative: ambiguous evidence should delay or suppress an alert rather than confidently assign the wrong road or direction.

## Four-level runtime model

1. **Level 1 - High-confidence OSM road graph**
   - current road match confidence >= high threshold
   - use graph connectivity, segment travel direction and camera road link
   - this is the preferred path

2. **Level 2 - Temporal multi-candidate OSM matching**
   - do not immediately abandon OSM when two or more nearby roads are plausible
   - retain the best 2-5 road candidates over several GPS updates
   - score candidate continuity, projection distance, heading agreement and previous-road connectivity
   - switch roads only when the new candidate wins by a configured margin for several samples

3. **Level 3 - GPS trajectory camera matching**
   - used when no OSM candidate reaches a reliable confidence
   - build a short recent trajectory from valid GPS points
   - calculate along-track position, cross-track distance, smoothed heading and approach trend for camera candidates
   - combine runtime geometry with offline camera metadata hints

4. **Level 4 - Classic GPS bearing fallback**
   - final insurance path only
   - require forward bearing agreement plus repeated decreasing camera distance
   - never let one noisy GPS sample trigger a direction decision

## Trajectory state

Maintain approximately 5-10 seconds of recent valid locations. Ignore or down-weight samples with poor accuracy, very low speed or insufficient movement.

Recommended derived values:

- smoothed vehicle heading from multiple moving samples
- current speed and displacement quality
- GPS accuracy quality
- camera distance history
- along-track distance to camera
- cross-track distance from the current trajectory axis
- bearing difference between vehicle motion and camera vector
- number of consecutive approach samples

Initial experimental thresholds live in `config/fallback_matching.json`. They are tuning parameters, not immutable constants.

## Temporal OSM candidate scoring

When OSM returns several plausible nearby roads, keep their identities over time instead of selecting the nearest segment independently at every update.

Useful evidence:

- projection distance to candidate polyline
- heading alignment with candidate segment orientation
- continuity with the previously matched segment/way
- whether movement along the candidate is physically continuous
- bridge/tunnel/layer consistency
- one-way compatibility
- persistence across multiple GPS samples

The system should resist rapid A-B-A-B road switching near parallel roads and intersections.

## Trajectory-only camera scoring

The trajectory fallback operates without requiring a valid road graph match.

Positive evidence:

- camera is ahead along the motion axis
- camera distance decreases over consecutive updates
- small heading/bearing difference
- small cross-track distance
- parsed `location_description` A->B direction hint agrees
- nearby opposite-direction camera-pair metadata agrees

Negative evidence:

- camera distance is increasing
- large cross-track distance consistent with a parallel road
- camera is behind or nearly opposite to vehicle heading
- GPS accuracy is poor
- evidence changes erratically between samples

Do not use any single soft metadata field as a hard direction filter.

## Offline fallback metadata

`derive_fallback_metadata.py` adds soft evidence to the camera DB:

- `location_from_hint`
- `location_to_hint`
- `location_hint_confidence`
- `location_hint_raw`
- `opposite_camera_id`
- `opposite_pair_distance_m`
- `opposite_pair_confidence`
- `opposite_pair_id`

### Location-description arrows

Descriptions such as `A -> B`, `A → B` or equivalent arrow forms are parsed into origin/destination text. These strings are retained as hints. They are not converted into a compass bearing unless a future geocoding step resolves both endpoints confidently.

### Opposite-direction camera pairs

Nearby cameras on the same normalized road name with opposite public road-direction codes can be paired when they are reciprocal nearest neighbors. Pairing is a soft clue for local direction/lane structure, not proof of the exact driven carriageway.

## Runtime decision policy

Suggested high-level behavior:

```text
if roadMatch.confidence >= OSM_HIGH:
    use Level 1
elif roadCandidates are present:
    update temporal candidates
    if candidate becomes stable:
        use Level 2
    else:
        evaluate Level 3 in parallel
else:
    use Level 3

if Level 3 remains insufficient:
    use Level 4 conservative fallback
```

For camera direction, hard exclusions are allowed only from reliable topology/direction metadata already marked as hard. All fallback text/pair/roadside evidence must remain score features.

## Alert-state machine

A camera candidate should move through states rather than alerting from a single frame:

`SEEN -> TRACKING -> CONFIRMED -> ALERTED -> PASSED`

Possible transitions:

- `SEEN`: first candidate observation
- `TRACKING`: candidate persists and approach trend is forming
- `CONFIRMED`: confidence/score threshold met for required consecutive samples
- `ALERTED`: warning threshold crossed and alert emitted once
- `PASSED`: along-track position or distance trend shows camera has been passed

If evidence collapses before `CONFIRMED`, drop the candidate silently.

## False-positive priorities

Particular cases that require test coverage:

- two parallel urban roads 20-100 m apart
- divided carriageways with cameras on both sides
- expressway and frontage/service road
- overpass/underpass crossing at similar coordinates
- interchange ramps
- sharp bends where simple camera bearing differs from road direction
- stop-and-go traffic / GPS heading instability
- U-turn or route reversal
- new road absent from OSM
- camera public coordinates displaced from the physical road

## Telemetry required for real-world tuning

Android V2 should be able to save an optional local diagnostic trace containing no account secrets:

- timestamp
- location lat/lon rounded only as needed for debugging
- GPS accuracy and speed
- smoothed heading
- selected OSM candidate and top alternatives with scores
- fallback level
- camera candidate IDs and component scores
- alert / suppress decision

This enables repeatable tuning from real drives rather than guessing threshold values.

## Acceptance criteria before calling fallback mature

- no one-sample direction decisions
- parallel-road rejection demonstrably better than classic distance+bearing
- candidate road switching is temporally stable
- unresolved evidence falls back rather than becoming a hard false assertion
- V1 classic engine remains available as final fallback
- every threshold is centralized/configurable
- offline camera metadata derivation is deterministic and validated in CI
