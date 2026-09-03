#!/usr/bin/env python3
import json
import math
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import osmium

EARTH_R = 6371000.0


def haversine(lat1, lon1, lat2, lon2):
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


def parse_cfg(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def is_drivable(tags, cfg):
    highway = tags.get("highway", "")
    if not highway:
        return False
    if highway in cfg["nonVehicleHighwayValues"]:
        return False
    mv = tags.get("motor_vehicle") or tags.get("motorcar")
    if mv in cfg["explicitMotorVehicleDeny"]:
        return False
    return True


def parse_oneway(tags):
    v = (tags.get("oneway") or "").lower()
    if v in {"yes", "1", "true"}:
        return 1
    if v == "-1":
        return -1
    if tags.get("junction") == "roundabout":
        return 1
    return 0


def zigzag(v):
    return (v << 1) ^ (v >> 63)


def put_varint(buf, value):
    value = int(value)
    while value >= 0x80:
        buf.append((value & 0x7F) | 0x80)
        value >>= 7
    buf.append(value)


def encode_geometry(points):
    # 1e-7 degree integer coordinates, delta + zigzag + varint.
    # This preserves OSM coordinate precision while avoiding verbose WKT/JSON.
    buf = bytearray()
    put_varint(buf, len(points))
    prev_lat = 0
    prev_lon = 0
    for i, (lat, lon) in enumerate(points):
        lat_i = int(round(lat * 10_000_000))
        lon_i = int(round(lon * 10_000_000))
        if i == 0:
            dlat, dlon = lat_i, lon_i
        else:
            dlat, dlon = lat_i - prev_lat, lon_i - prev_lon
        put_varint(buf, zigzag(dlat))
        put_varint(buf, zigzag(dlon))
        prev_lat, prev_lon = lat_i, lon_i
    return bytes(buf)


def ensure_schema(conn):
    conn.executescript(
        """
        PRAGMA page_size=4096;
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        PRAGMA temp_store=MEMORY;
        PRAGMA locking_mode=EXCLUSIVE;

        CREATE TABLE metadata(
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        ) WITHOUT ROWID;

        CREATE TABLE ways(
          way_id INTEGER PRIMARY KEY,
          highway TEXT NOT NULL,
          name TEXT,
          ref TEXT,
          maxspeed TEXT,
          oneway INTEGER NOT NULL,
          bridge INTEGER NOT NULL,
          tunnel INTEGER NOT NULL,
          layer INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE segments(
          segment_id INTEGER PRIMARY KEY,
          way_id INTEGER NOT NULL,
          from_node INTEGER NOT NULL,
          to_node INTEGER NOT NULL,
          length_m REAL NOT NULL,
          point_count INTEGER NOT NULL,
          geometry BLOB NOT NULL
        );

        CREATE INDEX idx_segments_way ON segments(way_id);
        CREATE INDEX idx_segments_from ON segments(from_node);
        CREATE INDEX idx_segments_to ON segments(to_node);

        CREATE VIRTUAL TABLE segment_rtree USING rtree(
          segment_id,
          min_lon, max_lon,
          min_lat, max_lat
        );
        """
    )


class RefCollector(osmium.SimpleHandler):
    def __init__(self, cfg, out_path):
        super().__init__()
        self.cfg = cfg
        self.out = open(out_path, "w", encoding="ascii", buffering=1024 * 1024)
        self.total_ways = 0
        self.drivable_ways = 0
        self.excluded_ways = 0
        self.refs_written = 0

    def way(self, w):
        self.total_ways += 1
        tags = {t.k: t.v for t in w.tags}
        if not is_drivable(tags, self.cfg):
            self.excluded_ways += 1
            return
        self.drivable_ways += 1
        for n in w.nodes:
            self.out.write(f"{n.ref}\n")
            self.refs_written += 1

    def close(self):
        self.out.close()


class CompactBuilder(osmium.SimpleHandler):
    def __init__(self, cfg, junctions, conn):
        super().__init__()
        self.cfg = cfg
        self.junctions = junctions
        self.conn = conn
        self.next_segment_id = 1
        self.way_rows = []
        self.segment_rows = []
        self.rtree_rows = []
        self.ways = 0
        self.segments = 0
        self.points = 0
        self.geom_bytes = 0

    def flush(self):
        if self.way_rows:
            self.conn.executemany(
                "INSERT INTO ways VALUES (?,?,?,?,?,?,?,?,?)",
                self.way_rows,
            )
            self.way_rows.clear()
        if self.segment_rows:
            self.conn.executemany(
                "INSERT INTO segments VALUES (?,?,?,?,?,?,?)",
                self.segment_rows,
            )
            self.conn.executemany(
                "INSERT INTO segment_rtree VALUES (?,?,?,?,?)",
                self.rtree_rows,
            )
            self.segment_rows.clear()
            self.rtree_rows.clear()

    def way(self, w):
        tags = {t.k: t.v for t in w.tags}
        if not is_drivable(tags, self.cfg):
            return
        nodes = list(w.nodes)
        if len(nodes) < 2:
            return
        try:
            coords = [(n.ref, n.location.lat, n.location.lon) for n in nodes]
        except Exception:
            return

        oneway = parse_oneway(tags)
        try:
            layer = int(tags.get("layer", "0"))
        except ValueError:
            layer = 0
        bridge = 1 if (tags.get("bridge") and tags.get("bridge") != "no") else 0
        tunnel = 1 if (tags.get("tunnel") and tags.get("tunnel") != "no") else 0

        self.way_rows.append((
            int(w.id),
            tags.get("highway", ""),
            tags.get("name"),
            tags.get("ref"),
            tags.get("maxspeed"),
            oneway,
            bridge,
            tunnel,
            layer,
        ))
        self.ways += 1

        split = [0]
        for i in range(1, len(coords) - 1):
            if coords[i][0] in self.junctions:
                split.append(i)
        split.append(len(coords) - 1)

        # De-duplicate split positions for closed/degenerate ways.
        clean_split = [split[0]]
        for idx in split[1:]:
            if idx > clean_split[-1]:
                clean_split.append(idx)

        for a, b in zip(clean_split, clean_split[1:]):
            part = coords[a:b + 1]
            if len(part) < 2:
                continue
            point_pairs = [(lat, lon) for _, lat, lon in part]
            length = 0.0
            for (_, lat1, lon1), (_, lat2, lon2) in zip(part, part[1:]):
                length += haversine(lat1, lon1, lat2, lon2)
            if length <= 0.01:
                continue

            lats = [p[1] for p in part]
            lons = [p[2] for p in part]
            geom = encode_geometry(point_pairs)
            sid = self.next_segment_id
            self.next_segment_id += 1

            self.segment_rows.append((
                sid,
                int(w.id),
                int(part[0][0]),
                int(part[-1][0]),
                length,
                len(part),
                sqlite3.Binary(geom),
            ))
            self.rtree_rows.append((
                sid,
                min(lons), max(lons),
                min(lats), max(lats),
            ))
            self.segments += 1
            self.points += len(part)
            self.geom_bytes += len(geom)

        if len(self.segment_rows) >= 25000 or len(self.way_rows) >= 25000:
            self.flush()


def load_junctions(path):
    junctions = set()
    with open(path, "r", encoding="ascii") as f:
        for line in f:
            line = line.strip()
            if line:
                junctions.add(int(line))
    return junctions


def main():
    if len(sys.argv) != 4:
        print("usage: build_compact_graph.py roads.osm.pbf vehicle_access.json output.db", file=sys.stderr)
        return 2

    pbf, cfg_path, out_db = map(Path, sys.argv[1:])
    cfg = parse_cfg(cfg_path)
    # Convert lists once so membership checks stay cheap on millions of ways.
    cfg["nonVehicleHighwayValues"] = set(cfg["nonVehicleHighwayValues"])
    cfg["explicitMotorVehicleDeny"] = set(cfg["explicitMotorVehicleDeny"])

    out_db.parent.mkdir(parents=True, exist_ok=True)
    if out_db.exists():
        out_db.unlink()

    with tempfile.TemporaryDirectory() as td_raw:
        td = Path(td_raw)
        refs = td / "node-refs.txt"
        junction_file = td / "junctions.txt"

        collector = RefCollector(cfg, refs)
        collector.apply_file(str(pbf))
        collector.close()

        env = os.environ.copy()
        env["LC_ALL"] = "C"
        # A junction is an OSM node referenced by at least two drivable ways.
        # sort/uniq keeps the large reference-counting job out of Python RAM.
        subprocess.run(
            f"sort -n --parallel=2 -S 50% '{refs}' | uniq -d > '{junction_file}'",
            shell=True,
            check=True,
            env=env,
        )
        junctions = load_junctions(junction_file)

        conn = sqlite3.connect(out_db)
        ensure_schema(conn)
        conn.execute("BEGIN")

        builder = CompactBuilder(cfg, junctions, conn)
        # sparse_mem_array uses about 16 bytes per input node before overhead and
        # preserves real OSM node IDs, avoiding false graph connections at bridges.
        builder.apply_file(str(pbf), locations=True, idx="sparse_mem_array")
        builder.flush()

        metadata = {
            "schemaVersion": "3",
            "graphType": "junction-split-polyline",
            "coordinatePrecision": "1e-7 degrees",
            "geometryEncoding": "delta-zigzag-varint",
            "sourcePolicy": "all plausible motor-vehicle OSM highway classes; not motorway-only",
            "turnRestrictions": "not-yet-applied",
        }
        conn.executemany("INSERT INTO metadata VALUES (?,?)", metadata.items())
        conn.commit()
        conn.execute("ANALYZE")
        conn.commit()

        stats = {
            "sourceHighwayWays": collector.total_ways,
            "drivableWays": collector.drivable_ways,
            "excludedNonVehicleWays": collector.excluded_ways,
            "nodeReferences": collector.refs_written,
            "junctionNodes": len(junctions),
            "storedWays": builder.ways,
            "segments": builder.segments,
            "geometryPoints": builder.points,
            "geometryBytes": builder.geom_bytes,
            "databaseBytes": out_db.stat().st_size,
        }
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
