# Camera Ninja V2 - Codex implementation specification

Status: authoritative implementation brief for integrating the Phase 2/3 road-camera data into the existing Android V1 app.

This document is the primary instruction set for Codex. Existing design documents remain supporting references:

- `docs/android-integration-contract.md`
- `docs/fallback-matching-plan.md`
- `docs/phase3-camera-matching.md`
- `config/fallback_matching.json`
- `config/reliability_policy.json`

If this document conflicts with an older design note, follow this document unless the conflict concerns the binary DB schema itself; in that case inspect the current generator scripts and DB metadata before coding.

---

## 1. Product objective

Camera Ninja is a general Android driving-assistance app that warns about speed cameras and related enforcement cameras. It is **not Tesla-specific**.

Primary reliability objective:

> Minimize false negatives for speed-camera warnings. A plausible speed camera must not silently disappear merely because OSM road matching, camera direction, GPS quality, or metadata confidence is imperfect.

A tolerable extra warning is preferable to silently missing a plausible speed camera. However, the app must still reduce obvious parallel-road and opposite-direction false positives using high-confidence evidence.

This is a driver-assistance tool, not a guarantee against traffic enforcement. Do not represent the app as 100% complete or infallible.

---

## 2. Non-negotiable implementation rules

1. **Extend the existing V1 app. Do not rewrite it as a new application architecture.**
2. Preserve the proven Bluetooth lifecycle, foreground service, location acquisition, TTS, audio ducking, settings, camera-data update behaviour and disconnect debounce unless this specification explicitly changes them.
3. Do not remove the V1 GPS-based alert engine. It remains the final fallback.
4. Do not simplify the road/fallback algorithm merely to make tests easier.
5. Do not silently drop any source speed camera.
6. Do not treat low-confidence direction metadata as a hard exclusion.
7. All runtime thresholds must be centralized/configurable, not scattered as magic numbers.
8. Avoid expensive work when monitoring is inactive.
9. Keep network/API secrets out of the APK and repository.
10. All user-facing text must be natural Korean unless a technical identifier genuinely must remain in English.

---

## 3. V1 product changes required before/while integrating V2

### 3.1 Rename the app to `Camera Ninja`

The launcher label, app title, notification title, settings title and all other user-visible product-name references must use:

`Camera Ninja`

Do not display the old product name anywhere in the UI.

The existing Android `applicationId` may remain unchanged if changing it would prevent in-place upgrade of the already-installed V1 app. An `applicationId` rename is a compatibility decision, not a cosmetic change. If Codex changes the package/application ID, it must explicitly document that this installs as a separate app and must preserve/migrate settings if practical.

### 3.2 Remove Tesla-specific language

Camera Ninja is vehicle-agnostic. Remove user-facing references to:

- `Tesla`
- `테슬라`
- Tesla-specific registration/setup wording

Replace them with generic vehicle wording, for example:

- `테슬라 등록` -> `차량 등록`
- `테슬라 블루투스` -> `차량 블루투스`
- `테슬라 연결 시 자동 시작` -> `등록한 차량이 연결되면 자동으로 시작`
- `등록된 테슬라` -> `등록된 차량`
- `Tesla connection` -> `차량 연결`

Also rename Tesla-specific internal class/function/resource identifiers where doing so is safe and does not create needless migration risk. Examples:

- `TeslaCompanionManager` -> `VehicleCompanionManager`
- Tesla-specific UI state names -> vehicle-generic names

Do not perform a large refactor solely for naming. Preserve behaviour first.

Acceptance check:

- Search user-visible resources and Compose text for case-insensitive `tesla` and Korean `테슬라`.
- There must be no Tesla wording in normal UI, notifications, settings, onboarding or accessibility text.
- Any remaining occurrence must be justified as a compatibility-only internal identifier and documented.

### 3.3 Natural Korean UI wording

Review all current V1 strings. Replace translation-like or developer-centric wording with concise natural Korean.

Preferred wording style:

| Avoid | Prefer |
|---|---|
| Bluetooth 설정 | 차량 블루투스 설정 |
| Companion 등록 | 차량 등록 |
| Monitoring Start | 모니터링 시작 |
| Monitoring Stop | 모니터링 중지 |
| Warning Distance | 알림 거리 |
| Warning Volume | 알림 음량 |
| Duck audio | 안내 중 다른 소리 줄이기 |
| TTS Test | 음성 안내 테스트 |
| Camera Data | 단속카메라 데이터 |
| Manual Update | 지금 업데이트 |
| Data Date | 데이터 기준일 |
| Location Permission | 위치 권한 |
| Foreground Service | 사용자 화면에 기술용어로 노출하지 말 것 |

Rules:

- Prefer short Korean phrases that a non-developer driver understands immediately.
- Do not expose implementation terms such as R-tree, segment, fallback, foreground service, Room, worker, confidence score in the normal UI.
- Technical diagnostics may use those terms only in a dedicated developer/diagnostic screen.
- Keep spacing, particles and verb endings natural; avoid machine-translated noun piles.
- Reuse string resources instead of hard-coded Compose strings wherever practical.

### 3.4 Continuous overspeed chime near a speed camera

When approaching an eligible speed camera while the vehicle is currently above the known camera speed limit, Camera Ninja must keep warning the driver with a repeating `띵-띵-띵` chime pattern.

Eligibility:

- camera class is `SPEED` or `SPEED_SIGNAL`
- camera is still an active ahead-candidate, not passed
- camera is within the configured alert/active approach range
- camera `speed_limit` is known and greater than zero
- current vehicle speed is greater than the camera speed limit

Required behaviour:

1. Initial camera TTS warning still works normally.
2. If overspeed remains true, play a short three-ding pattern repeatedly.
3. Recommended pattern: three short dings about 250-350 ms apart, followed by about 1.2-1.8 s pause, then repeat.
4. Stop the repeating chime when any of these becomes true:
   - speed is at or below the limit for at least 2 consecutive valid speed samples
   - camera is passed
   - camera candidate is invalidated
   - monitoring stops
   - Bluetooth vehicle disconnect stops the monitoring session
5. If TTS is currently speaking, it is acceptable to pause or duck the chime during the utterance, but the chime must resume immediately afterwards if overspeed is still true.
6. Do not permanently request exclusive audio focus for the chime. Use navigation-guidance-compatible audio attributes and respect the existing alert volume setting.
7. Do not play the overspeed chime when `speed_limit` is `NULL`/unknown.
8. Do not treat public-data speed `0` as a real speed limit.
9. The overspeed chime state must be tied to the physical camera/alert state so duplicate source rows do not produce overlapping chime loops.
10. The chime must stop immediately when the service/session is stopped to avoid leaked audio.

Implementation preference:

- A lightweight `SoundPool`/short-audio implementation or equivalent is preferred for low latency.
- Use a bundled, license-safe sound asset or a programmatically generated neutral tone. Do not add copyrighted assets from unknown sources.
- Maintain one chime controller/state machine rather than spawning repeated coroutines/tasks per GPS update.

Suggested interface:

```kotlin
interface OverspeedChimeController {
    fun update(
        cameraKey: String?,
        isCameraActive: Boolean,
        vehicleSpeedKmh: Double?,
        cameraSpeedLimitKmh: Int?,
        ttsSpeaking: Boolean,
    )
    fun stop()
}
```

Unit-test at least:

- overspeed starts chime
- speed returns below limit -> chime stops after required stable samples
- unknown limit -> no chime
- passed camera -> stops
- monitoring stop/disconnect -> stops
- duplicate source records in same physical cluster -> only one chime loop
- TTS temporarily interrupts/ducks but does not permanently cancel an ongoing overspeed warning

---

## 4. Existing V1 behaviours that must remain working

Preserve all existing proven behaviour unless a real bug is found:

- registered vehicle Bluetooth connection starts monitoring
- Bluetooth disconnect uses the existing debounce/recheck behaviour (currently about 7 seconds)
- disconnect stops location updates, road matching, camera tracking, TTS and overspeed chime
- foreground notification exists only while monitoring
- app does not try to toggle Android system Location globally
- app only requests/removes its own location updates
- TTS remains Korean/offline-capable where the device supports it
- audio ducking remains available
- TTS test remains available
- warning distance remains configurable
- warning volume remains configurable
- public camera-data update remains fault-tolerant: failed update must not destroy the working DB
- existing manual monitoring controls remain available for testing

Do not regress V1 while introducing road matching.

---

## 5. Runtime data files

Android keeps a matched version pair:

- `camera-ninja-korea-roads-compact.db`
- `camera-ninja-korea-cameras.db`

The road and camera DBs are a coupled snapshot. Never independently install a camera DB built against a different road snapshot.

Road DB important tables:

- `metadata`
- `ways`
- `segments`
- `segment_rtree`

Camera DB contains source camera fields plus Phase 3 matching/direction/fallback/reliability metadata.

Important camera fields include:

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
- `location_from_hint`, `location_to_hint`
- `location_hint_confidence`
- `opposite_camera_id`
- `opposite_pair_confidence`
- reliability/sanity/cluster metadata produced by the current Phase 3 pipeline

Inspect the current generator scripts and actual SQLite schema before final DAO implementation. Do not rely only on this prose if the schema has evolved.

---

## 6. Road geometry decoder

Road geometry encoding is:

`1e-7 degree integer -> delta -> zigzag -> unsigned varint BLOB`

Decoder:

1. read point count unsigned varint
2. for every point, read delta-lat and delta-lon unsigned varints
3. zigzag-decode deltas
4. accumulate integer latitude/longitude
5. divide by `10_000_000.0`

Create a focused unit-tested decoder. Include fixtures containing:

- positive/negative deltas
- at least two multi-point polylines
- coordinates around Korea
- malformed/truncated BLOB handling

Malformed geometry must not crash the foreground service. Treat that segment as unusable and continue with other candidates/fallback.

---

## 7. Vehicle road matching

Create separable modules, conceptually similar to:

- `RoadDatabase`
- `RoadGeometryDecoder`
- `RoadMatcher`
- `TemporalRoadMatcher`
- `RoadGraphNavigator`
- `RoadCameraRepository`
- `RoadCameraAlertEngine`
- `FallbackCameraAlertEngine`
- `RuntimeDataUpdater`

Names may follow the existing project conventions.

For every valid moving GPS sample:

1. query `segment_rtree` near current GPS
2. decode candidate polylines
3. project GPS onto candidates
4. score candidates by:
   - perpendicular distance
   - heading alignment
   - previous-segment continuity
   - previous-way continuity
   - graph connectivity
   - road class compatibility
   - bridge/tunnel/layer consistency
   - OSM one-way legality
5. keep the previous road unless a new candidate wins by a meaningful configured margin for multiple samples
6. produce a road-match result containing at least:
   - segment ID
   - way ID
   - offset
   - travel sign
   - confidence
   - candidate alternatives for diagnostics

Do not choose a new segment independently from scratch on every GPS frame. Parallel-road flapping must be resisted temporally.

---

## 8. Four-level camera decision engine

### Level 1 - High-confidence OSM graph

Use when road match is high confidence.

- traverse connected road graph forward
- find cameras on current/forward reachable segments
- rank by graph distance ahead
- apply hard direction filtering only under Section 9

### Level 2 - Temporal multi-candidate OSM

Use when multiple nearby roads are plausible.

- retain top 2-5 candidates over several samples
- score persistence/continuity/heading/projection/graph compatibility
- do not rapidly switch parallel roads
- if one candidate becomes stable, promote it
- evaluate Level 3 in parallel when warning-window timing makes waiting unsafe

### Level 3 - GPS trajectory camera matching

Use when OSM is missing or not reliable enough.

Maintain a recent moving trajectory, roughly 5-10 seconds, using valid location samples.

Evaluate plausible camera candidates using:

- along-track distance
- cross-track distance
- smoothed vehicle heading
- bearing to camera
- heading difference
- repeated decreasing distance / approach trend
- GPS accuracy
- public `A -> B` location-description hints
- opposite-direction camera-pair metadata
- physical cluster state

No single soft field may hard-suppress a speed camera.

### Level 4 - Classic GPS fallback

Keep the V1 classic engine as final insurance.

At minimum retain:

- distance to camera
- bearing-to-camera
- smoothed heading
- approach trend over repeated samples
- one alert per physical camera/session

This path must remain available even when OSM fails completely.

---

## 9. Direction filtering policy

`travel_sign` meanings relative to OSM segment orientation:

- `+1`: OSM `from_node -> to_node`
- `-1`: reverse
- `0`: both
- `NULL`: unresolved

A camera may be rejected solely because of direction only when both conditions are true:

1. vehicle road match is high confidence
2. camera `direction_hard_filter == 1`

Recommended rule:

```text
if roadMatch.confidence >= 0.80 && camera.direction_hard_filter == 1:
    require vehicleTravelSign == camera.travel_sign
else:
    do not reject solely on camera direction
```

If direction is unresolved, keep the camera eligible and let trajectory/fallback evidence decide whether it remains plausible.

Do not convert Korean public road direction code `1/2/3` directly into compass direction.

---

## 10. Zero-loss speed-camera reliability policy

For `SPEED` and `SPEED_SIGNAL` source records:

- source record retention must be 100%
- OSM failure must not make a valid-coordinate speed camera disappear
- low match confidence must not make it disappear
- low direction confidence must not make it disappear
- duplicate physical clustering must not delete source rows
- suspicious but still Korea-plausible coordinates are flagged, not silently discarded
- objectively out-of-Korea coordinates may be quarantined, but must appear in diagnostics/build reports

Runtime rule:

> `OSM failure != alert failure`

Every source speed camera must have an explainable runtime category:

- road-primary
- temporal-road candidate
- trajectory-fallback
- classic-fallback
- quarantined for objectively invalid coordinate

No silent state is allowed.

---

## 11. Physical camera clustering

Multiple public records may represent the same physical enforcement point.

Use the reliability metadata/cluster policy generated offline. Approximate cluster radius is currently around 10 m.

Clustering is for:

- duplicate voice suppression
- duplicate chime suppression
- shared alert/session state

Clustering is **not** permission to delete source records or merge away opposite-direction evidence before direction evaluation.

A physical cluster should emit at most one simultaneous alert/chime for the same approach event unless there is strong evidence of genuinely separate enforcement devices requiring distinct warnings.

---

## 12. Camera alert state machine

Use persistent per-session candidate state rather than one-frame decisions.

Minimum states:

`SEEN -> TRACKING -> CONFIRMED -> ALERTED -> PASSED`

Expected behaviour:

- `SEEN`: first plausible observation
- `TRACKING`: candidate persists; approach evidence accumulating
- `CONFIRMED`: confidence sufficient for the chosen level
- `ALERTED`: warning threshold crossed and normal camera warning emitted
- `PASSED`: graph/trajectory/distance evidence confirms camera passed

Because speed-camera false negatives are the higher-risk failure mode, an OSM-uncertain camera near the alert threshold must not remain indefinitely in `TRACKING` until the warning opportunity is gone. Escalate to Level 3/4 fallback early enough to preserve the alert opportunity.

---

## 13. Warning-distance semantics

Existing configurable warning distance remains the main user setting.

Road-primary mode:

- use graph distance ahead where reliable

Fallback modes:

- use along-track/straight-line distance as appropriate

Threshold crossing should not repeatedly retrigger for GPS jitter.

The overspeed chime may continue after the initial TTS alert while the same camera remains active and overspeed persists.

---

## 14. Audio and TTS

Preserve the existing TTS and audio-focus behaviour.

Requirements:

- Korean TTS remains default
- navigation-guidance audio attributes
- existing ducking option remains functional
- warning-volume setting affects camera alert audio consistently
- TTS test button works
- no multiple overlapping TTS utterances for duplicate records
- overspeed chime does not spawn multiple overlapping loops
- all TTS/chime resources stop when monitoring stops

Natural example announcements:

- known speed: `700미터 앞 과속단속카메라입니다. 제한속도 60킬로미터입니다.`
- unknown speed: `700미터 앞 과속단속카메라입니다.`

Do not announce `제한속도 0`.

Exact sentence may remain user-customizable if V1 already supports a template, but default Korean must sound natural.

---

## 15. Bluetooth lifecycle must become vehicle-generic

The existing automatic lifecycle is useful and must remain, but all naming must be generic.

Expected behaviour:

```text
registered vehicle Bluetooth connects
    -> monitoring/service starts
    -> location updates start
    -> road/fallback matching active

vehicle Bluetooth disconnects
    -> existing debounce/recheck
    -> if still disconnected, monitoring stops
    -> location updates stop
    -> road/fallback candidate state clears
    -> TTS stops
    -> overspeed chime stops
    -> foreground notification removed
```

Support any user-selected/registered Bluetooth vehicle device; do not enforce Tesla name/manufacturer filters.

---

## 16. Runtime data update

Road DB + camera DB are an atomic pair.

Safe update sequence:

1. fetch manifest
2. select a mutually compatible road/camera version
3. download both compressed files
4. verify SHA-256
5. decompress to temporary filenames
6. validate SQLite integrity
7. validate schema/metadata compatibility
8. validate expected record counts and speed-camera retention metadata
9. close current DB handles
10. atomically swap both files
11. reopen
12. run a small road R-tree + camera sanity query
13. only then delete old pair

Failure at any step leaves the previous working pair intact.

Do not put the Data.go.kr API key in the APK. The phone consumes the already-produced runtime artifact/manifest, not the secret build API.

---

## 17. Background/service performance

Target phone is a modern Galaxy-class Android device, but the design should still avoid waste.

Requirements:

- no road DB work when monitoring inactive
- do not load the full road DB into RAM
- spatial query only near current GPS
- decode only candidate polylines
- cache current/nearby candidate geometry when useful
- reuse prepared SQLite statements
- perform DB I/O off the main thread
- keep Compose/UI state independent from heavy matching state
- avoid uncontrolled coroutine creation per GPS sample
- foreground-service lifetime owns/cleans matching and audio resources

---

## 18. Diagnostics for real-drive tuning

Add an optional developer/diagnostic trace mode. Default can be off.

For each useful sample log locally:

- timestamp
- GPS lat/lon
- accuracy
- speed
- smoothed heading
- selected road candidate
- top alternative road candidates and component scores
- fallback level
- active camera IDs/physical cluster IDs
- camera score components
- direction-hard-filter decision
- alert/suppress/fallback reason
- current alert-state-machine state
- overspeed chime start/stop reason

Do not log account secrets or API credentials.

Provide a simple export/share mechanism only if it is easy and does not destabilize core implementation; otherwise a file accessible through app diagnostics is sufficient for the first version.

---

## 19. Required tests

### Unit tests

At minimum cover:

- geometry varint/zigzag decoding
- projection onto polyline
- circular heading difference
- along-track/cross-track calculation
- temporal candidate stability
- one-way handling
- hard direction filtering rule
- low-confidence direction does not hard-suppress
- speed `0` becomes unknown
- physical camera cluster de-duplication
- alert-state transitions
- overspeed chime state transitions
- natural default TTS text formation
- DB-pair compatibility validation

### Scenario tests

Create deterministic fixtures for:

- two parallel urban roads 20-100 m apart
- divided road with opposite-direction cameras
- expressway vs frontage/service road
- overpass/underpass crossing
- interchange ramp
- curved road where straight bearing is misleading
- stop-and-go / noisy GPS heading
- U-turn / route reversal
- new road absent from OSM
- camera coordinate displaced from the road
- OSM camera match `LOW`/`REVIEW`
- no OSM candidate
- suspicious-but-Korea-plausible coordinate
- duplicate public camera rows at one physical site
- approach while over speed limit, then slowing below limit

### Regression tests

Ensure V1 behaviours still pass after V2 integration:

- vehicle Bluetooth auto-start
- disconnect debounce
- service stop
- TTS
- audio ducking
- warning settings
- manual monitoring
- camera-data update failure retention

---

## 20. Build/quality gate

Before considering implementation complete, Codex must run everything available in the project, including as applicable:

- Gradle unit tests
- Android lint
- debug APK build
- any instrumentation tests that can run in the environment
- static search for forbidden user-visible Tesla wording
- database/decoder tests

Do not weaken a failing test just to make the suite green. Fix the implementation or document a real environment limitation.

---

## 21. Implementation order for Codex

Do not attempt a single giant rewrite. Work in this order and keep the app buildable after each stage:

### Stage A - V1 cleanup

- app label -> `Camera Ninja`
- natural Korean strings
- vehicle-generic Bluetooth/UI wording
- remove Tesla wording
- implement/test continuous overspeed chime
- verify V1 regression suite

### Stage B - Runtime DB foundation

- road/camera DB file management
- schema reader
- geometry decoder
- R-tree nearby segment DAO
- DB compatibility/atomic update validation

### Stage C - Road matching

- GPS projection
- candidate scoring
- temporal candidate persistence
- travel-sign result
- diagnostic output

### Stage D - Graph camera lookup

- graph-forward traversal
- camera-ahead query
- segment-offset handling
- conservative junction behaviour
- hard direction filter only when permitted

### Stage E - Fallback engine

- short GPS trajectory
- along/cross-track
- approach trend
- A->B and opposite-pair soft evidence
- Level 2/3/4 arbitration
- alert-state machine

### Stage F - Integration

- plug into existing foreground service
- preserve Bluetooth lifecycle
- preserve V1 final fallback
- physical cluster de-duplication
- overspeed chime linked to active physical camera

### Stage G - validation

- all tests
- lint/build
- scenario fixtures
- report performance and unresolved risks

---

## 22. Expected final architecture

Conceptually:

```text
Vehicle Bluetooth
      |
      v
Foreground monitoring service
      |
      +--> Location samples
              |
              v
        RoadMatcher
              |
        +-----+--------------------------+
        |                                |
   high confidence                 ambiguous/no OSM
        |                                |
        v                                v
RoadGraphNavigator              TemporalRoadMatcher
        |                                |
        v                                v
RoadCameraRepository          TrajectoryFallback
        |                                |
        +---------------+----------------+
                        |
                        v
               Camera candidate state
            SEEN/TRACKING/CONFIRMED
                        |
                        v
                    ALERTED
                  /           \
                TTS       Overspeed chime
                  \           /
                        v
                      PASSED
```

Classic V1 GPS alert logic remains available underneath as the final safety net.

---

## 23. Codex must report at completion

Return a concise implementation report containing:

1. changed files
2. new modules/classes
3. V1 strings/product-name changes
4. every remaining `Tesla/테슬라/tesla` occurrence and why it remains
5. road DB integration status
6. camera DB integration status
7. fallback levels implemented
8. overspeed chime behaviour implemented
9. tests executed and results
10. APK/build artifact location if available
11. known limitations / items requiring real-drive tuning
12. any decision that deviated from this spec and the exact reason

Do not claim road-matching or camera-warning reliability that has not been demonstrated by tests or real-drive data.

---

## 24. Definition of done

Camera Ninja V2 is implementation-complete only when:

- app is visibly named `Camera Ninja`
- normal UI is natural Korean
- normal UI contains no Tesla-specific wording
- any user-registered vehicle Bluetooth device can drive the lifecycle
- existing V1 lifecycle/TTS/settings behaviour is preserved
- road/camera DB pair can be loaded safely
- high-confidence OSM road matching works
- temporal OSM fallback works
- GPS trajectory fallback works
- classic V1 GPS fallback still exists
- speed cameras are not silently dropped because OSM/direction confidence is low
- physical duplicates do not create duplicate simultaneous alerts
- approaching an eligible known-limit speed camera while overspeeding produces a repeating `띵-띵-띵` warning until the stop conditions are met
- all available unit/lint/build checks pass
- unresolved real-world tuning items are explicitly documented
