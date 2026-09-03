#!/usr/bin/env python3
import argparse
import collections
import hashlib
import json
import math
import re
import sqlite3
import statistics
import time
from pathlib import Path

SPEED_CODES = {"1", "01"}
SIGNAL_CODES = {"2", "02"}
SPEED_SIGNAL_CODES = {"01+02", "1+2", "1+02", "01+2"}


def normalize_text(value):
    if value is None:
        return ""
    return re.sub(r"[^0-9A-Za-z가-힣]+", "", str(value)).lower()


def normalize_route_ref(value):
    if value is None:
        return ""
    return "".join(re.findall(r"\d+", str(value)))


def normalize_direction(value):
    value = str(value or "").strip()
    if value in {"01", "1"}:
        return "1"
    if value in {"02", "2"}:
        return "2"
    if value in {"03", "3"}:
        return "3"
    return value


def normalize_enforcement(code):
    code = str(code or "").strip()
    if code in SPEED_CODES:
        return "SPEED"
    if code in SIGNAL_CODES:
        return "SIGNAL"
    if code in SPEED_SIGNAL_CODES:
        return "SPEED_SIGNAL"
    return "OTHER"


def parse_int(value):
    try:
        if value is None or str(value).strip() == "":
            return None
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


def parse_float(value):
    try:
        if value is None or str(value).strip() == "":
            return None
        return float(str(value))
    except (TypeError, ValueError):
        return None


def stable_camera_id(record):
    identity = "|".join(
        [
            str(record.get("제공기관코드") or ""),
            str(record.get("무인교통단속카메라관리번호") or ""),
            str(record.get("위도") or ""),
            str(record.get("경도") or ""),
            str(record.get("단속구분") or ""),
        ]
    )
    return hashlib.sha1(identity.encode("utf-8")).hexdigest()


def decode_varint(buf, pos):
    value = 0
    shift = 0
    while pos < len(buf):
        b = buf[pos]
        pos += 1
        value |= (b & 0x7F) << shift
        if b < 0x80:
            return value, pos
        shift += 7
    raise ValueError("truncated varint")


def unzigzag(value):
    return (value >> 1) ^ -(value & 1)


def decode_geometry(blob):
    pos = 0
    count, pos = decode_varint(blob, pos)
    lat_i = 0
    lon_i = 0
    points = []
    for _ in range(count):
        dlat, pos = decode_varint(blob, pos)
        dlon, pos = decode_varint(blob, pos)
        lat_i += unzigzag(dlat)
        lon_i += unzigzag(dlon)
        points.append((lat_i / 10_000_000.0, lon_i / 10_000_000.0))
    return points


def bearing_deg(lat1, lon1, lat2, lon2):
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def project_to_polyline(lat, lon, points):
    if len(points) < 2:
        return None

    # Local equirectangular projection is sufficient for sub-kilometre matching.
    cos_lat = max(0.2, math.cos(math.radians(lat)))
    kx = 111_320.0 * cos_lat
    ky = 110_574.0
    xy = [((plon - lon) * kx, (plat - lat) * ky) for plat, plon in points]

    leg_lengths = []
    total_length = 0.0
    for (x1, y1), (x2, y2) in zip(xy, xy[1:]):
        length = math.hypot(x2 - x1, y2 - y1)
        leg_lengths.append(length)
        total_length += length

    best = None
    cumulative = 0.0
    for i, ((x1, y1), (x2, y2), length) in enumerate(zip(xy, xy[1:], leg_lengths)):
        if length < 1e-9:
            continue
        dx = x2 - x1
        dy = y2 - y1
        t = max(0.0, min(1.0, (-(x1 * dx + y1 * dy)) / (length * length)))
        qx = x1 + t * dx
        qy = y1 + t * dy
        distance = math.hypot(qx, qy)
        along = cumulative + t * length
        cross = dx * (-y1) - dy * (-x1)
        side = 1 if cross > 0 else (-1 if cross < 0 else 0)

        if best is None or distance < best["distance"]:
            snap_lat = points[i][0] + t * (points[i + 1][0] - points[i][0])
            snap_lon = points[i][1] + t * (points[i + 1][1] - points[i][1])
            best = {
                "distance": distance,
                "offset": along / max(total_length, 1e-9),
                "snap_lat": snap_lat,
                "snap_lon": snap_lon,
                "bearing": bearing_deg(
                    points[i][0], points[i][1], points[i + 1][0], points[i + 1][1]
                ),
                "side": side,
            }
        cumulative += length

    return best


def candidate_bbox(lat, lon, radius_m):
    dlat = radius_m / 110_574.0
    dlon = radius_m / (111_320.0 * max(0.2, math.cos(math.radians(lat))))
    return lon - dlon, lon + dlon, lat - dlat, lat + dlat


def fetch_candidates(conn, lat, lon, radius_m):
    min_lon, max_lon, min_lat, max_lat = candidate_bbox(lat, lon, radius_m)
    return conn.execute(
        """
        SELECT
          s.segment_id, s.way_id, s.from_node, s.to_node,
          s.length_m, s.geometry,
          w.highway, w.name, w.ref, w.maxspeed, w.oneway,
          w.bridge, w.tunnel, w.layer
        FROM segment_rtree r
        JOIN segments s ON s.segment_id = r.segment_id
        JOIN ways w ON w.way_id = s.way_id
        WHERE r.min_lon <= ? AND r.max_lon >= ?
          AND r.min_lat <= ? AND r.max_lat >= ?
        """,
        (max_lon, min_lon, max_lat, min_lat),
    ).fetchall()


def road_class_penalty(camera_road_type, osm_highway):
    if camera_road_type == "고속국도" and osm_highway not in {"motorway", "motorway_link"}:
        return 25.0
    if camera_road_type == "일반국도" and osm_highway in {
        "service",
        "track",
        "path",
        "residential",
    }:
        return 12.0
    return 0.0


def confidence_for(distance, name_exact, name_contains, ref_match, score_gap):
    if distance <= 8:
        confidence = 0.92
    elif distance <= 15:
        confidence = 0.88
    elif distance <= 25:
        confidence = 0.82
    elif distance <= 40:
        confidence = 0.74
    elif distance <= 60:
        confidence = 0.64
    elif distance <= 90:
        confidence = 0.52
    elif distance <= 130:
        confidence = 0.40
    elif distance <= 200:
        confidence = 0.28
    else:
        confidence = 0.12

    if name_exact:
        confidence += 0.07
    elif name_contains:
        confidence += 0.03
    if ref_match:
        confidence += 0.04

    if score_gap is not None:
        if score_gap < 3:
            confidence -= 0.16
        elif score_gap < 8:
            confidence -= 0.10
        elif score_gap < 15:
            confidence -= 0.05

    return max(0.0, min(1.0, confidence))


def confidence_label(value):
    if value >= 0.80:
        return "HIGH"
    if value >= 0.60:
        return "MEDIUM"
    if value >= 0.40:
        return "LOW"
    return "REVIEW"


def match_camera(conn, record, search_radius_m, expanded_radius_m, max_snap_m):
    try:
        lat = float(record["위도"])
        lon = float(record["경도"])
    except (KeyError, TypeError, ValueError):
        return None, "INVALID_COORD"

    camera_name = normalize_text(record.get("도로노선명"))
    camera_ref = normalize_route_ref(record.get("도로노선번호"))
    camera_road_type = str(record.get("도로종류") or "")

    rows = fetch_candidates(conn, lat, lon, search_radius_m)
    if not rows:
        rows = fetch_candidates(conn, lat, lon, expanded_radius_m)
    if not rows:
        return None, "NO_CANDIDATE"

    scored = []
    for row in rows:
        (
            segment_id,
            way_id,
            from_node,
            to_node,
            segment_length,
            geometry,
            highway,
            osm_name,
            osm_ref,
            osm_maxspeed,
            oneway,
            bridge,
            tunnel,
            layer,
        ) = row

        projection = project_to_polyline(lat, lon, decode_geometry(geometry))
        if projection is None:
            continue

        normalized_osm_name = normalize_text(osm_name)
        normalized_osm_ref = normalize_route_ref(osm_ref)
        name_exact = bool(camera_name and normalized_osm_name and camera_name == normalized_osm_name)
        name_contains = bool(
            camera_name
            and normalized_osm_name
            and not name_exact
            and (camera_name in normalized_osm_name or normalized_osm_name in camera_name)
        )
        ref_match = bool(camera_ref and normalized_osm_ref and camera_ref == normalized_osm_ref)

        score = projection["distance"]
        if name_exact:
            score -= 35.0
        elif name_contains:
            score -= 12.0
        if ref_match:
            score -= 20.0
        score += road_class_penalty(camera_road_type, highway)

        scored.append(
            {
                "score": score,
                "distance": projection["distance"],
                "segment_id": segment_id,
                "way_id": way_id,
                "from_node": from_node,
                "to_node": to_node,
                "segment_length": segment_length,
                "segment_offset": projection["offset"],
                "snap_lat": projection["snap_lat"],
                "snap_lon": projection["snap_lon"],
                "segment_bearing": projection["bearing"],
                "side": projection["side"],
                "highway": highway,
                "osm_name": osm_name,
                "osm_ref": osm_ref,
                "osm_maxspeed": osm_maxspeed,
                "oneway": oneway,
                "bridge": bridge,
                "tunnel": tunnel,
                "layer": layer,
                "name_exact": name_exact,
                "name_contains": name_contains,
                "ref_match": ref_match,
            }
        )

    if not scored:
        return None, "NO_CANDIDATE"

    scored.sort(key=lambda item: (item["score"], item["distance"]))
    best = scored[0]
    if best["distance"] > max_snap_m:
        return None, "TOO_FAR"

    score_gap = scored[1]["score"] - best["score"] if len(scored) > 1 else None
    confidence = confidence_for(
        best["distance"],
        best["name_exact"],
        best["name_contains"],
        best["ref_match"],
        score_gap,
    )

    best["candidate_count"] = len(scored)
    best["score_gap"] = score_gap
    best["confidence"] = confidence
    best["status"] = confidence_label(confidence)
    return best, None


def ensure_camera_schema(conn):
    conn.executescript(
        """
        DROP TABLE IF EXISTS cameras;
        DROP TABLE IF EXISTS camera_metadata;

        CREATE TABLE camera_metadata(
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        ) WITHOUT ROWID;

        CREATE TABLE cameras(
          camera_id TEXT PRIMARY KEY,
          source_camera_no TEXT,
          provider_code TEXT,
          provider_name TEXT,
          province TEXT,
          city_district TEXT,
          road_type TEXT,
          road_route_no TEXT,
          road_name TEXT,
          road_direction_raw TEXT,
          road_direction_norm TEXT,
          latitude REAL NOT NULL,
          longitude REAL NOT NULL,
          location_description TEXT,
          enforcement_code_raw TEXT,
          enforcement_class TEXT NOT NULL,
          alert_default INTEGER NOT NULL,
          speed_limit INTEGER,
          section_position_raw TEXT,
          section_length_m REAL,
          protection_zone_raw TEXT,
          installed_year TEXT,
          data_date TEXT,
          segment_id INTEGER,
          segment_way_id INTEGER,
          segment_from_node INTEGER,
          segment_to_node INTEGER,
          segment_offset REAL,
          snap_distance_m REAL,
          snap_lat REAL,
          snap_lon REAL,
          segment_bearing_deg REAL,
          side_of_segment INTEGER,
          osm_highway TEXT,
          osm_name TEXT,
          osm_ref TEXT,
          osm_maxspeed TEXT,
          osm_oneway INTEGER,
          osm_bridge INTEGER,
          osm_tunnel INTEGER,
          osm_layer INTEGER,
          match_confidence REAL,
          match_status TEXT NOT NULL,
          candidate_count INTEGER,
          candidate_score_gap REAL,
          match_reason TEXT
        );

        CREATE INDEX idx_cameras_segment ON cameras(segment_id);
        CREATE INDEX idx_cameras_way_nodes
          ON cameras(segment_way_id, segment_from_node, segment_to_node);
        CREATE INDEX idx_cameras_enforcement ON cameras(enforcement_class);
        CREATE INDEX idx_cameras_alert_default ON cameras(alert_default);
        CREATE INDEX idx_cameras_match_status ON cameras(match_status);
        """
    )


def camera_row(record, match, error):
    enforcement_class = normalize_enforcement(record.get("단속구분"))
    alert_default = 1 if enforcement_class in {"SPEED", "SPEED_SIGNAL"} else 0
    speed_limit = parse_int(record.get("제한속도"))
    if speed_limit == 0:
        speed_limit = None

    common = [
        stable_camera_id(record),
        record.get("무인교통단속카메라관리번호"),
        record.get("제공기관코드"),
        record.get("제공기관명"),
        record.get("시도명"),
        record.get("시군구명"),
        record.get("도로종류"),
        record.get("도로노선번호"),
        record.get("도로노선명"),
        record.get("도로노선방향"),
        normalize_direction(record.get("도로노선방향")),
        float(record.get("위도")),
        float(record.get("경도")),
        record.get("설치장소"),
        record.get("단속구분"),
        enforcement_class,
        alert_default,
        speed_limit,
        record.get("단속구간위치구분"),
        parse_float(record.get("과속단속구간길이")),
        record.get("보호구역구분"),
        record.get("설치연도"),
        record.get("데이터기준일자"),
    ]

    if error:
        return common + [
            None, None, None, None, None, None, None, None, None, None,
            None, None, None, None, None, None, None, None, None,
            0.0, error, 0, None, error,
        ]

    reason_bits = []
    if match["name_exact"]:
        reason_bits.append("road_name_exact")
    elif match["name_contains"]:
        reason_bits.append("road_name_partial")
    if match["ref_match"]:
        reason_bits.append("route_ref_match")
    reason_bits.append("nearest_polyline")

    return common + [
        match["segment_id"],
        match["way_id"],
        match["from_node"],
        match["to_node"],
        match["segment_offset"],
        match["distance"],
        match["snap_lat"],
        match["snap_lon"],
        match["segment_bearing"],
        match["side"],
        match["highway"],
        match["osm_name"],
        match["osm_ref"],
        match["osm_maxspeed"],
        match["oneway"],
        match["bridge"],
        match["tunnel"],
        match["layer"],
        match["confidence"],
        match["status"],
        match["candidate_count"],
        match["score_gap"],
        "+".join(reason_bits),
    ]


def load_records(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict) and isinstance(data.get("records"), list):
        return data["records"]
    if isinstance(data, list):
        return data
    raise ValueError("camera JSON must be a list or an object containing records[]")


def build_report(records, status_counts, distances, enforcement_counts, elapsed_s, output_db):
    sorted_distances = sorted(distances)

    def percentile(q):
        if not sorted_distances:
            return None
        index = int((len(sorted_distances) - 1) * q)
        return sorted_distances[index]

    return {
        "sourceRecords": len(records),
        "matchStatus": dict(status_counts),
        "enforcementClasses": dict(enforcement_counts),
        "matchedRecords": len(distances),
        "unmatchedRecords": len(records) - len(distances),
        "snapDistanceMeters": {
            "mean": statistics.mean(distances) if distances else None,
            "p50": percentile(0.50),
            "p90": percentile(0.90),
            "p95": percentile(0.95),
            "p99": percentile(0.99),
            "max": max(distances) if distances else None,
        },
        "elapsedSeconds": elapsed_s,
        "outputDatabaseBytes": output_db.stat().st_size,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("road_db", type=Path)
    parser.add_argument("camera_json", type=Path)
    parser.add_argument("output_db", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--search-radius-m", type=float, default=120.0)
    parser.add_argument("--expanded-radius-m", type=float, default=300.0)
    parser.add_argument("--max-snap-m", type=float, default=250.0)
    args = parser.parse_args()

    records = load_records(args.camera_json)
    args.output_db.parent.mkdir(parents=True, exist_ok=True)
    if args.output_db.exists():
        args.output_db.unlink()

    road = sqlite3.connect(f"file:{args.road_db}?mode=ro", uri=True)
    road.execute("PRAGMA mmap_size=536870912")
    road.execute("PRAGMA cache_size=-200000")

    out = sqlite3.connect(args.output_db)
    out.execute("PRAGMA journal_mode=OFF")
    out.execute("PRAGMA synchronous=OFF")
    out.execute("PRAGMA temp_store=MEMORY")
    ensure_camera_schema(out)
    out.execute("BEGIN")

    insert_sql = "INSERT INTO cameras VALUES (" + ",".join(["?"] * 46) + ")"
    rows = []
    status_counts = collections.Counter()
    enforcement_counts = collections.Counter()
    distances = []
    started = time.time()

    for index, record in enumerate(records, 1):
        enforcement_counts[normalize_enforcement(record.get("단속구분"))] += 1
        match, error = match_camera(
            road,
            record,
            args.search_radius_m,
            args.expanded_radius_m,
            args.max_snap_m,
        )
        if error:
            status_counts[error] += 1
        else:
            status_counts[match["status"]] += 1
            distances.append(match["distance"])

        rows.append(camera_row(record, match, error))
        if len(rows) >= 2000:
            out.executemany(insert_sql, rows)
            rows.clear()

        if index % 5000 == 0:
            print(f"matched {index}/{len(records)}", flush=True)

    if rows:
        out.executemany(insert_sql, rows)

    out.executemany(
        "INSERT INTO camera_metadata VALUES (?,?)",
        [
            ("schemaVersion", "1"),
            ("source", "Korea Public Data Portal nationwide unmanned traffic camera standard data"),
            ("roadDirectionPolicy", "raw code preserved; never interpreted as compass azimuth"),
            ("defaultAlertClasses", "SPEED,SPEED_SIGNAL"),
            ("speedLimitZeroPolicy", "stored as NULL/unknown"),
            ("roadReferencePolicy", "segment_id convenience + way/from/to stable fallback fields"),
        ],
    )
    out.commit()
    out.execute("ANALYZE")
    out.commit()

    elapsed = time.time() - started
    report = build_report(
        records,
        status_counts,
        distances,
        enforcement_counts,
        elapsed,
        args.output_db,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    out.close()
    road.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
