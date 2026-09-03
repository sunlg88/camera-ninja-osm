#!/usr/bin/env python3
"""Derive conservative camera travel-direction metadata.

This script intentionally separates hard direction filters from weak roadside hints.
The Korean public road-direction code (1/2/3) is preserved as route-direction metadata;
it is not treated as a compass bearing. OSM way orientation is also not assumed to
match the official Korean route start/end direction.

Direction evidence priority:
1. OSM one-way topology: high confidence.
2. Public bidirectional code on a two-way road: applies both directions.
3. Same-OSM-way side calibration using both public direction codes: medium/high.
4. Camera roadside position relative to the OSM polyline: weak hint only.
5. Otherwise unresolved; Android must fall back to runtime GPS/heading logic.
"""

import argparse
import collections
import json
import sqlite3
from pathlib import Path


CALIBRATION_MIN_MATCH_CONFIDENCE = 0.75
CALIBRATION_MIN_SNAP_M = 3.0
CALIBRATION_MAX_SNAP_M = 25.0
SIDE_HINT_MIN_SNAP_M = 5.0
SIDE_HINT_MAX_SNAP_M = 30.0
SIDE_HINT_MIN_MATCH_CONFIDENCE = 0.70


def ensure_column(conn, table, name, decl):
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if name not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def ensure_schema(conn):
    ensure_column(conn, "cameras", "direction_mode", "TEXT")
    ensure_column(conn, "cameras", "travel_sign", "INTEGER")
    ensure_column(conn, "cameras", "direction_confidence", "REAL")
    ensure_column(conn, "cameras", "direction_evidence", "TEXT")
    ensure_column(conn, "cameras", "direction_hard_filter", "INTEGER NOT NULL DEFAULT 0")

    conn.executescript(
        """
        DROP TABLE IF EXISTS camera_direction_calibration;
        CREATE TABLE camera_direction_calibration(
          way_id INTEGER PRIMARY KEY,
          code1_side INTEGER,
          code1_samples INTEGER NOT NULL,
          code1_dominance REAL,
          code2_side INTEGER,
          code2_samples INTEGER NOT NULL,
          code2_dominance REAL,
          calibration_confidence REAL NOT NULL,
          hard_filter_eligible INTEGER NOT NULL,
          evidence TEXT NOT NULL
        );
        """
    )


def dominant(counter):
    if not counter:
        return None, 0, 0.0
    side, count = counter.most_common(1)[0]
    total = sum(counter.values())
    return side, total, count / total


def build_calibrations(conn):
    rows = conn.execute(
        """
        SELECT segment_way_id, road_direction_norm, side_of_segment,
               snap_distance_m, match_confidence
        FROM cameras
        WHERE segment_way_id IS NOT NULL
          AND road_direction_norm IN ('1','2')
          AND osm_oneway = 0
          AND side_of_segment IN (-1,1)
          AND snap_distance_m BETWEEN ? AND ?
          AND match_confidence >= ?
        """,
        (
            CALIBRATION_MIN_SNAP_M,
            CALIBRATION_MAX_SNAP_M,
            CALIBRATION_MIN_MATCH_CONFIDENCE,
        ),
    )

    grouped = collections.defaultdict(lambda: {"1": collections.Counter(), "2": collections.Counter()})
    for way_id, code, side, _snap, _match_conf in rows:
        grouped[way_id][code][side] += 1

    calibrations = {}
    inserts = []
    for way_id, by_code in grouped.items():
        side1, n1, dom1 = dominant(by_code["1"])
        side2, n2, dom2 = dominant(by_code["2"])
        if not n1 or not n2:
            continue
        if side1 != -side2:
            continue
        if dom1 < 0.75 or dom2 < 0.75:
            continue

        # One clean camera for each public direction already provides useful local
        # evidence, but repeated observations raise confidence substantially.
        conf = 0.68
        if n1 + n2 >= 4:
            conf += 0.06
        if min(n1, n2) >= 2:
            conf += 0.06
        if dom1 >= 0.90 and dom2 >= 0.90:
            conf += 0.05
        if dom1 == 1.0 and dom2 == 1.0:
            conf += 0.03
        conf = min(0.88, conf)
        hard = 1 if conf >= 0.78 else 0
        evidence = (
            f"same_way_opposite_public_codes;"
            f"code1_side={side1},n={n1},dominance={dom1:.3f};"
            f"code2_side={side2},n={n2},dominance={dom2:.3f}"
        )
        calibrations[way_id] = {
            "1": side1,
            "2": side2,
            "confidence": conf,
            "hard": hard,
        }
        inserts.append((way_id, side1, n1, dom1, side2, n2, dom2, conf, hard, evidence))

    conn.executemany(
        """
        INSERT INTO camera_direction_calibration VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        inserts,
    )
    return calibrations


def classify(row, calibrations):
    (
        segment_way_id,
        public_dir,
        oneway,
        side,
        snap_m,
        match_conf,
        match_status,
    ) = row

    if segment_way_id is None or match_status in {"INVALID_COORD", "NO_CANDIDATE", "TOO_FAR"}:
        return "UNMATCHED", None, 0.0, "no reliable OSM segment", 0

    oneway = int(oneway or 0)
    side = int(side or 0)
    public_dir = str(public_dir or "")
    snap_m = float(snap_m) if snap_m is not None else None
    match_conf = float(match_conf or 0.0)

    # OSM topology is the strongest directional fact in the local road graph.
    if oneway in (-1, 1):
        sign = oneway
        if public_dir == "3":
            return (
                "PUBLIC_BOTH_OSM_ONEWAY",
                sign,
                0.78,
                "public direction says both; OSM segment is one-way; topology wins at runtime",
                0,
            )
        if public_dir in {"1", "2"}:
            return "OSM_ONEWAY", sign, 0.98, "OSM one-way topology", 1
        return "OSM_ONEWAY_PUBLIC_UNKNOWN", sign, 0.82, "OSM one-way topology; public direction unknown", 1

    if public_dir == "3":
        return "PUBLIC_BIDIRECTIONAL", 0, 0.98, "public route direction is bidirectional", 0

    if public_dir not in {"1", "2"}:
        return "PUBLIC_DIRECTION_UNKNOWN", None, 0.10, "public direction code missing/unknown", 0

    calibration = calibrations.get(segment_way_id)
    if calibration:
        expected_side = calibration[public_dir]
        if side == expected_side:
            # South Korea drives on the right. For an OSM-forward segment, a camera
            # physically on the right side is a forward-direction hint; left side is
            # a reverse-direction hint. Same-way opposite-code calibration is required
            # before this can become a hard filter.
            sign = -side
            conf = calibration["confidence"]
            hard = calibration["hard"]
            return (
                "WAY_SIDE_CALIBRATED",
                sign,
                conf,
                f"same-way public-code calibration; camera_side={side}; right-hand-traffic inference",
                hard,
            )
        return (
            "WAY_SIDE_CALIBRATION_CONFLICT",
            None,
            0.30,
            f"camera side {side} conflicts with calibrated side {expected_side}",
            0,
        )

    if (
        side in (-1, 1)
        and snap_m is not None
        and SIDE_HINT_MIN_SNAP_M <= snap_m <= SIDE_HINT_MAX_SNAP_M
        and match_conf >= SIDE_HINT_MIN_MATCH_CONFIDENCE
    ):
        sign = -side
        # This is deliberately below the hard-filter threshold. It is useful as a
        # score feature together with live vehicle heading, not as a sole exclusion.
        conf = min(0.62, 0.48 + 0.12 * match_conf)
        return (
            "ROADSIDE_HINT",
            sign,
            conf,
            f"camera is {snap_m:.1f} m from centerline on side {side}; soft hint only",
            0,
        )

    return (
        "UNRESOLVED_TWO_WAY",
        None,
        0.20,
        "two-way road without reliable local direction evidence",
        0,
    )


def derive(conn):
    calibrations = build_calibrations(conn)
    rows = conn.execute(
        """
        SELECT camera_id, segment_way_id, road_direction_norm, osm_oneway,
               side_of_segment, snap_distance_m, match_confidence, match_status
        FROM cameras
        """
    ).fetchall()

    updates = []
    counts = collections.Counter()
    hard_counts = collections.Counter()
    for camera_id, *fields in rows:
        mode, sign, confidence, evidence, hard = classify(tuple(fields), calibrations)
        counts[mode] += 1
        hard_counts[hard] += 1
        updates.append((mode, sign, confidence, evidence, hard, camera_id))

    conn.executemany(
        """
        UPDATE cameras
        SET direction_mode=?, travel_sign=?, direction_confidence=?,
            direction_evidence=?, direction_hard_filter=?
        WHERE camera_id=?
        """,
        updates,
    )

    conn.execute("DROP INDEX IF EXISTS idx_cameras_direction_mode")
    conn.execute("DROP INDEX IF EXISTS idx_cameras_travel_sign")
    conn.execute("DROP INDEX IF EXISTS idx_cameras_direction_hard_filter")
    conn.execute("CREATE INDEX idx_cameras_direction_mode ON cameras(direction_mode)")
    conn.execute("CREATE INDEX idx_cameras_travel_sign ON cameras(travel_sign)")
    conn.execute("CREATE INDEX idx_cameras_direction_hard_filter ON cameras(direction_hard_filter)")

    conn.execute(
        "INSERT OR REPLACE INTO camera_metadata VALUES (?,?)",
        ("directionSchemaVersion", "1"),
    )
    conn.execute(
        "INSERT OR REPLACE INTO camera_metadata VALUES (?,?)",
        (
            "directionRuntimePolicy",
            "hard-filter only when direction_hard_filter=1; roadside hints are scoring features only",
        ),
    )
    conn.commit()

    single_total = conn.execute(
        "SELECT COUNT(*) FROM cameras WHERE road_direction_norm IN ('1','2') AND segment_id IS NOT NULL"
    ).fetchone()[0]
    single_hard = conn.execute(
        """
        SELECT COUNT(*) FROM cameras
        WHERE road_direction_norm IN ('1','2')
          AND segment_id IS NOT NULL
          AND direction_hard_filter=1
        """
    ).fetchone()[0]
    default_total = conn.execute("SELECT COUNT(*) FROM cameras WHERE alert_default=1").fetchone()[0]
    default_hard = conn.execute(
        "SELECT COUNT(*) FROM cameras WHERE alert_default=1 AND direction_hard_filter=1"
    ).fetchone()[0]

    return {
        "cameraCount": len(rows),
        "calibratedWays": len(calibrations),
        "directionModes": dict(sorted(counts.items())),
        "hardFilterCount": hard_counts[1],
        "singleDirectionMatched": single_total,
        "singleDirectionHardResolved": single_hard,
        "singleDirectionHardResolvedRatio": (single_hard / single_total) if single_total else 0.0,
        "defaultAlertCameras": default_total,
        "defaultAlertHardResolved": default_hard,
        "defaultAlertHardResolvedRatio": (default_hard / default_total) if default_total else 0.0,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("camera_db", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    conn = sqlite3.connect(args.camera_db)
    ensure_schema(conn)
    report = derive(conn)
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    report["integrity"] = integrity
    conn.close()

    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text, encoding="utf-8")
    return 0 if integrity == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
