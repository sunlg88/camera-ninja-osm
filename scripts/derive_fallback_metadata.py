#!/usr/bin/env python3
"""Derive soft fallback metadata for Camera Ninja.

This script does NOT create hard travel-direction decisions. It extracts structured
A->B text hints from public camera location descriptions and identifies conservative
nearby opposite-direction camera pairs. Android may use these as score features only.
"""

import argparse
import collections
import hashlib
import json
import math
import re
import sqlite3
from pathlib import Path

ARROW_RE = re.compile(
    r"([가-힣A-Za-z0-9·._\-/\s]{2,60}?)\s*(?:→|->|⇒|➜|➡|＞|>)\s*([가-힣A-Za-z0-9·._\-/\s]{2,60})"
)


def ensure_column(conn, table, name, decl):
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if name not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def clean_hint(value):
    value = re.sub(r"\s+", " ", value or "").strip(" -_/.,")
    # Keep the nearest phrase around the arrow instead of unrelated site prose.
    if "(" in value:
        value = value.rsplit("(", 1)[-1].strip()
    if ")" in value:
        value = value.split(")", 1)[0].strip()
    return value[:80]


def extract_arrow_hint(text):
    if not text:
        return None
    matches = list(ARROW_RE.finditer(str(text)))
    if not matches:
        return None
    # Prefer the most compact match; long prose around an arrow is usually noisier.
    match = min(matches, key=lambda m: len(m.group(0)))
    origin = clean_hint(match.group(1))
    destination = clean_hint(match.group(2))
    if len(origin) < 2 or len(destination) < 2 or origin == destination:
        return None
    confidence = 0.82
    if "(" in str(text) and ")" in str(text):
        confidence += 0.05
    if len(origin) <= 30 and len(destination) <= 30:
        confidence += 0.04
    return origin, destination, min(confidence, 0.92), match.group(0).strip()


def normalize_road(value):
    if not value:
        return ""
    return re.sub(r"[^0-9A-Za-z가-힣]+", "", str(value)).lower()


def haversine(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def pair_confidence(distance_m, same_way, same_provider):
    if distance_m <= 20:
        c = 0.84
    elif distance_m <= 50:
        c = 0.76
    elif distance_m <= 90:
        c = 0.66
    else:
        c = 0.56
    if same_way:
        c += 0.08
    if same_provider:
        c += 0.03
    return min(c, 0.92)


def derive_text_hints(conn):
    ensure_column(conn, "cameras", "location_from_hint", "TEXT")
    ensure_column(conn, "cameras", "location_to_hint", "TEXT")
    ensure_column(conn, "cameras", "location_hint_confidence", "REAL")
    ensure_column(conn, "cameras", "location_hint_raw", "TEXT")

    updates = []
    parsed = 0
    for camera_id, description in conn.execute(
        "SELECT camera_id, location_description FROM cameras"
    ):
        hint = extract_arrow_hint(description)
        if hint:
            origin, destination, confidence, raw = hint
            parsed += 1
            updates.append((origin, destination, confidence, raw, camera_id))
        else:
            updates.append((None, None, None, None, camera_id))

    conn.executemany(
        """
        UPDATE cameras
        SET location_from_hint=?, location_to_hint=?,
            location_hint_confidence=?, location_hint_raw=?
        WHERE camera_id=?
        """,
        updates,
    )
    return parsed


def derive_opposite_pairs(conn, max_distance_m=120.0):
    ensure_column(conn, "cameras", "opposite_camera_id", "TEXT")
    ensure_column(conn, "cameras", "opposite_pair_distance_m", "REAL")
    ensure_column(conn, "cameras", "opposite_pair_confidence", "REAL")
    ensure_column(conn, "cameras", "opposite_pair_id", "TEXT")

    rows = conn.execute(
        """
        SELECT camera_id, latitude, longitude, road_name, road_direction_norm,
               segment_way_id, provider_code, match_confidence
        FROM cameras
        WHERE road_direction_norm IN ('1','2')
          AND latitude IS NOT NULL AND longitude IS NOT NULL
        """
    ).fetchall()

    # About 0.001 degree is ~111 m latitude. Search a 3x3 grid around each camera.
    cell_size = 0.001
    grid = collections.defaultdict(list)
    records = {}
    for row in rows:
        camera_id, lat, lon, road_name, direction, way_id, provider, match_conf = row
        road_key = normalize_road(road_name)
        if not road_key:
            continue
        rec = {
            "id": camera_id,
            "lat": float(lat),
            "lon": float(lon),
            "road": road_key,
            "dir": direction,
            "way": way_id,
            "provider": provider,
            "match_conf": float(match_conf or 0.0),
        }
        records[camera_id] = rec
        cell = (int(math.floor(rec["lat"] / cell_size)), int(math.floor(rec["lon"] / cell_size)))
        grid[(road_key, cell[0], cell[1])].append(rec)

    nearest = {}
    for camera_id, rec in records.items():
        cx = int(math.floor(rec["lat"] / cell_size))
        cy = int(math.floor(rec["lon"] / cell_size))
        best = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for other in grid.get((rec["road"], cx + dx, cy + dy), []):
                    if other["id"] == camera_id or other["dir"] == rec["dir"]:
                        continue
                    distance = haversine(rec["lat"], rec["lon"], other["lat"], other["lon"])
                    if distance > max_distance_m:
                        continue
                    if best is None or distance < best[0]:
                        best = (distance, other["id"])
        if best:
            nearest[camera_id] = best

    # Only retain reciprocal nearest neighbors. This avoids one camera being paired
    # to several opposite-direction cameras at a dense intersection.
    pairs = []
    updates = []
    paired_ids = set()
    for camera_id, (distance, other_id) in nearest.items():
        reverse = nearest.get(other_id)
        if not reverse or reverse[1] != camera_id:
            continue
        if camera_id in paired_ids or other_id in paired_ids:
            continue
        a = records[camera_id]
        b = records[other_id]
        same_way = a["way"] is not None and a["way"] == b["way"]
        same_provider = bool(a["provider"] and a["provider"] == b["provider"])
        confidence = pair_confidence(distance, same_way, same_provider)
        pair_id = hashlib.sha1("|".join(sorted([camera_id, other_id])).encode("utf-8")).hexdigest()
        updates.extend(
            [
                (other_id, distance, confidence, pair_id, camera_id),
                (camera_id, distance, confidence, pair_id, other_id),
            ]
        )
        pairs.append(
            {
                "pairId": pair_id,
                "a": camera_id,
                "b": other_id,
                "distanceM": distance,
                "sameOsmWay": same_way,
                "confidence": confidence,
            }
        )
        paired_ids.add(camera_id)
        paired_ids.add(other_id)

    conn.execute(
        """
        UPDATE cameras
        SET opposite_camera_id=NULL, opposite_pair_distance_m=NULL,
            opposite_pair_confidence=NULL, opposite_pair_id=NULL
        """
    )
    conn.executemany(
        """
        UPDATE cameras
        SET opposite_camera_id=?, opposite_pair_distance_m=?,
            opposite_pair_confidence=?, opposite_pair_id=?
        WHERE camera_id=?
        """,
        updates,
    )
    return pairs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("camera_db", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    conn = sqlite3.connect(args.camera_db)
    parsed = derive_text_hints(conn)
    pairs = derive_opposite_pairs(conn)
    conn.execute(
        "INSERT OR REPLACE INTO camera_metadata VALUES (?,?)",
        ("fallbackMetadataVersion", "1"),
    )
    conn.execute(
        "INSERT OR REPLACE INTO camera_metadata VALUES (?,?)",
        (
            "fallbackMetadataPolicy",
            "location arrow hints and opposite-camera pairs are soft score features only",
        ),
    )
    conn.commit()
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM cameras").fetchone()[0]
    conn.close()

    same_way_pairs = sum(1 for p in pairs if p["sameOsmWay"])
    report = {
        "integrity": integrity,
        "cameraCount": total,
        "locationArrowHints": parsed,
        "oppositeDirectionPairs": len(pairs),
        "oppositeDirectionPairedCameras": len(pairs) * 2,
        "sameOsmWayPairs": same_way_pairs,
        "policy": "soft evidence only; never a sole hard direction filter",
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text, encoding="utf-8")
    return 0 if integrity == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
