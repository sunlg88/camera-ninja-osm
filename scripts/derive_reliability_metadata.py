#!/usr/bin/env python3
"""Add reliability metadata without silently dropping speed cameras.

Safety objective: minimize false negatives. This stage flags bad coordinates and groups
physical duplicates, but preserves every source row. Only coordinates outside the Korea
bounding box are quarantined from automatic runtime alerts; suspicious in-Korea records
remain eligible for fallback matching.
"""

import argparse
import collections
import json
import math
import sqlite3
import statistics
from pathlib import Path


def ensure_column(conn, table, name, decl):
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if name not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def haversine(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def percentile(values, q):
    if not values:
        return 0.0
    values = sorted(values)
    idx = int((len(values) - 1) * q)
    return values[idx]


def in_bbox(lat, lon, bbox):
    return (
        bbox["minLat"] <= lat <= bbox["maxLat"]
        and bbox["minLon"] <= lon <= bbox["maxLon"]
    )


def load_source_records(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("records"), list):
        return data["records"]
    raise ValueError("source JSON must be a list or contain records[]")


def source_speed_class(code):
    code = str(code or "").strip()
    if code in {"1", "01", "1+2", "01+02", "1+02", "01+2"}:
        return True
    return False


def derive_coordinate_quality(conn, policy):
    bbox = policy["coordinateSanity"]["koreaBoundingBox"]
    min_group = int(policy["coordinateSanity"]["adminOutlierMinimumGroupSize"])
    min_dist = float(policy["coordinateSanity"]["adminOutlierMinimumDistanceMeters"])
    p95_mult = float(policy["coordinateSanity"]["adminOutlierP95Multiplier"])

    ensure_column(conn, "cameras", "coordinate_quality", "TEXT")
    ensure_column(conn, "cameras", "coordinate_outlier_distance_m", "REAL")
    ensure_column(conn, "cameras", "runtime_alert_eligible", "INTEGER NOT NULL DEFAULT 1")

    rows = conn.execute(
        "SELECT camera_id, province, city_district, latitude, longitude FROM cameras"
    ).fetchall()
    groups = collections.defaultdict(list)
    for camera_id, province, district, lat, lon in rows:
        groups[(province or "", district or "")].append((camera_id, float(lat), float(lon)))

    group_stats = {}
    for key, members in groups.items():
        if len(members) < min_group:
            continue
        center_lat = statistics.median([m[1] for m in members])
        center_lon = statistics.median([m[2] for m in members])
        distances = [haversine(center_lat, center_lon, m[1], m[2]) for m in members]
        p95 = percentile(distances, 0.95)
        threshold = max(min_dist, p95 * p95_mult)
        group_stats[key] = (center_lat, center_lon, threshold)

    updates = []
    counts = collections.Counter()
    for camera_id, province, district, lat, lon in rows:
        lat = float(lat)
        lon = float(lon)
        if not in_bbox(lat, lon, bbox):
            quality = "OUTSIDE_KOREA_BBOX"
            outlier_distance = None
            eligible = 0
        else:
            stat = group_stats.get((province or "", district or ""))
            if stat:
                center_lat, center_lon, threshold = stat
                distance = haversine(center_lat, center_lon, lat, lon)
                if distance > threshold:
                    quality = "ADMIN_OUTLIER_IN_KOREA"
                    outlier_distance = distance
                else:
                    quality = "OK"
                    outlier_distance = distance
            else:
                quality = "OK_NO_ADMIN_BASELINE"
                outlier_distance = None
            eligible = 1
        counts[quality] += 1
        updates.append((quality, outlier_distance, eligible, camera_id))

    conn.executemany(
        """
        UPDATE cameras
        SET coordinate_quality=?, coordinate_outlier_distance_m=?, runtime_alert_eligible=?
        WHERE camera_id=?
        """,
        updates,
    )
    return counts


class UnionFind:
    def __init__(self):
        self.parent = {}
        self.rank = {}

    def add(self, x):
        if x not in self.parent:
            self.parent[x] = x
            self.rank[x] = 0

    def find(self, x):
        p = self.parent[x]
        if p != x:
            self.parent[x] = self.find(p)
        return self.parent[x]

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1


def derive_physical_clusters(conn, policy):
    radius_m = float(policy["physicalClustering"]["radiusMeters"])
    ensure_column(conn, "cameras", "physical_cluster_id", "TEXT")
    ensure_column(conn, "cameras", "physical_cluster_size", "INTEGER")

    rows = conn.execute(
        """
        SELECT camera_id, latitude, longitude, enforcement_class, road_direction_norm
        FROM cameras
        WHERE runtime_alert_eligible=1
        """
    ).fetchall()

    # Cell size slightly larger than cluster radius; compare neighboring cells only.
    cell_deg = max(radius_m / 110574.0, 0.00005)
    grid = collections.defaultdict(list)
    uf = UnionFind()
    records = {}

    for camera_id, lat, lon, enforcement, direction in rows:
        lat = float(lat)
        lon = float(lon)
        rec = (camera_id, lat, lon, enforcement or "", direction or "")
        records[camera_id] = rec
        uf.add(camera_id)
        cx = int(math.floor(lat / cell_deg))
        cy = int(math.floor(lon / cell_deg))
        grid[(cx, cy)].append(rec)

    for camera_id, lat, lon, enforcement, direction in records.values():
        cx = int(math.floor(lat / cell_deg))
        cy = int(math.floor(lon / cell_deg))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for other_id, olat, olon, _oenforcement, _odirection in grid.get((cx + dx, cy + dy), []):
                    if other_id <= camera_id:
                        continue
                    if haversine(lat, lon, olat, olon) <= radius_m:
                        uf.union(camera_id, other_id)

    groups = collections.defaultdict(list)
    for camera_id in records:
        groups[uf.find(camera_id)].append(camera_id)

    updates = []
    multi_clusters = 0
    clustered_rows = 0
    max_size = 1
    for members in groups.values():
        members = sorted(members)
        cluster_id = "pc:" + members[0]
        size = len(members)
        max_size = max(max_size, size)
        if size > 1:
            multi_clusters += 1
            clustered_rows += size
        for camera_id in members:
            updates.append((cluster_id, size, camera_id))

    conn.executemany(
        "UPDATE cameras SET physical_cluster_id=?, physical_cluster_size=? WHERE camera_id=?",
        updates,
    )
    return {
        "clusters": len(groups),
        "multiMemberClusters": multi_clusters,
        "rowsInMultiMemberClusters": clustered_rows,
        "maxClusterSize": max_size,
    }


def validate_zero_loss(conn, source_records):
    source_total = len(source_records)
    source_speed = sum(1 for r in source_records if source_speed_class(r.get("단속구분")))

    db_total = conn.execute("SELECT COUNT(*) FROM cameras").fetchone()[0]
    db_speed = conn.execute("SELECT COUNT(*) FROM cameras WHERE alert_default=1").fetchone()[0]
    in_korea_speed = conn.execute(
        "SELECT COUNT(*) FROM cameras WHERE alert_default=1 AND runtime_alert_eligible=1"
    ).fetchone()[0]
    in_korea_speed_eligible = conn.execute(
        """
        SELECT COUNT(*) FROM cameras
        WHERE alert_default=1 AND runtime_alert_eligible=1
          AND coordinate_quality != 'OUTSIDE_KOREA_BBOX'
        """
    ).fetchone()[0]
    osm_failed_speed = conn.execute(
        """
        SELECT COUNT(*) FROM cameras
        WHERE alert_default=1 AND runtime_alert_eligible=1 AND segment_id IS NULL
        """
    ).fetchone()[0]
    osm_failed_speed_still_enabled = conn.execute(
        """
        SELECT COUNT(*) FROM cameras
        WHERE alert_default=1 AND runtime_alert_eligible=1 AND segment_id IS NULL
          AND match_status IN ('INVALID_COORD','NO_CANDIDATE','TOO_FAR')
        """
    ).fetchone()[0]
    unsafe_direction_suppression = conn.execute(
        """
        SELECT COUNT(*) FROM cameras
        WHERE alert_default=1
          AND direction_hard_filter=1
          AND COALESCE(direction_confidence,0) < 0.78
        """
    ).fetchone()[0]

    failures = []
    if db_total != source_total:
        failures.append(f"source row loss: source={source_total}, db={db_total}")
    if db_speed != source_speed:
        failures.append(f"speed camera row loss/reclassification: source={source_speed}, db={db_speed}")
    if in_korea_speed != in_korea_speed_eligible:
        failures.append("valid in-Korea speed camera was disabled")
    if osm_failed_speed != osm_failed_speed_still_enabled:
        failures.append("OSM failure silently removed a speed camera from fallback eligibility")
    if unsafe_direction_suppression:
        failures.append(f"unsafe low-confidence hard direction filters={unsafe_direction_suppression}")

    return {
        "sourceRecords": source_total,
        "databaseRecords": db_total,
        "sourceSpeedRelevant": source_speed,
        "databaseSpeedRelevant": db_speed,
        "eligibleInKoreaSpeed": in_korea_speed,
        "osmFailedEligibleSpeed": osm_failed_speed,
        "unsafeLowConfidenceHardFilters": unsafe_direction_suppression,
        "zeroLossGate": "PASS" if not failures else "FAIL",
        "failures": failures,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("camera_db", type=Path)
    parser.add_argument("source_json", type=Path)
    parser.add_argument("policy_json", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    source_records = load_source_records(args.source_json)
    policy = json.loads(args.policy_json.read_text(encoding="utf-8"))
    conn = sqlite3.connect(args.camera_db)

    coordinate_counts = derive_coordinate_quality(conn, policy)
    cluster_stats = derive_physical_clusters(conn, policy)
    conn.execute(
        "INSERT OR REPLACE INTO camera_metadata VALUES (?,?)",
        ("reliabilityPolicyVersion", str(policy.get("schemaVersion", 1))),
    )
    conn.execute(
        "INSERT OR REPLACE INTO camera_metadata VALUES (?,?)",
        ("reliabilityObjective", policy["objective"]),
    )
    conn.commit()

    gates = validate_zero_loss(conn, source_records)
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    conn.close()

    report = {
        "integrity": integrity,
        "coordinateQuality": dict(sorted(coordinate_counts.items())),
        "physicalClustering": cluster_stats,
        "gates": gates,
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text, encoding="utf-8")

    return 0 if integrity == "ok" and gates["zeroLossGate"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
