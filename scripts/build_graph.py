#!/usr/bin/env python3
import json
import math
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

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
    if highway in set(cfg["nonVehicleHighwayValues"]):
        return False
    mv = tags.get("motor_vehicle") or tags.get("motorcar")
    if mv in set(cfg["explicitMotorVehicleDeny"]):
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


def ensure_schema(conn):
    conn.executescript(
        """
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        PRAGMA temp_store=MEMORY;
        CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE nodes(
          node_id INTEGER PRIMARY KEY,
          lat REAL NOT NULL,
          lon REAL NOT NULL
        );
        CREATE TABLE edges(
          edge_id INTEGER PRIMARY KEY,
          way_id INTEGER NOT NULL,
          seq INTEGER NOT NULL,
          from_node INTEGER NOT NULL,
          to_node INTEGER NOT NULL,
          length_m REAL NOT NULL,
          highway TEXT,
          name TEXT,
          ref TEXT,
          maxspeed TEXT,
          oneway INTEGER NOT NULL,
          drivable INTEGER NOT NULL,
          access TEXT,
          motor_vehicle TEXT,
          motorcar TEXT,
          bridge TEXT,
          tunnel TEXT,
          layer TEXT,
          min_lat REAL NOT NULL,
          max_lat REAL NOT NULL,
          min_lon REAL NOT NULL,
          max_lon REAL NOT NULL
        );
        CREATE INDEX idx_edges_way ON edges(way_id);
        CREATE INDEX idx_edges_from ON edges(from_node);
        CREATE INDEX idx_edges_to ON edges(to_node);
        CREATE INDEX idx_edges_drivable ON edges(drivable);
        CREATE VIRTUAL TABLE edge_rtree USING rtree(
          edge_id,
          min_lon, max_lon,
          min_lat, max_lat
        );
        """
    )


def main():
    if len(sys.argv) != 4:
        print("usage: build_graph.py roads.osm.pbf vehicle_access.json output.db", file=sys.stderr)
        return 2

    pbf, cfg_path, out_db = map(Path, sys.argv[1:])
    cfg = parse_cfg(cfg_path)
    out_db.parent.mkdir(parents=True, exist_ok=True)
    if out_db.exists():
        out_db.unlink()

    conn = sqlite3.connect(out_db)
    ensure_schema(conn)

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        nodes_tsv = td / "nodes.tsv"
        ways_jsonseq = td / "ways.jsonseq"

        # Export nodes and ways in streaming-friendly formats via osmium.
        subprocess.run([
            "osmium", "export", str(pbf),
            "-f", "geojsonseq",
            "-o", str(ways_jsonseq),
            "--geometry-types=linestring"
        ], check=True)

        # Node table is intentionally minimal. osmium export does not emit referenced
        # OSM node ids for line vertices, so we use deterministic synthetic node ids
        # derived from rounded coordinates. This is sufficient for local map matching
        # and graph traversal; turn-restriction relation support is added in phase 2b.
        node_cache = {}
        next_node_id = 1
        next_edge_id = 1
        edge_rows = []
        rtree_rows = []
        node_rows = []

        def node_id(lat, lon):
            nonlocal next_node_id
            key = (round(lat, 7), round(lon, 7))
            v = node_cache.get(key)
            if v is None:
                v = next_node_id
                next_node_id += 1
                node_cache[key] = v
                node_rows.append((v, lat, lon))
                if len(node_rows) >= 50000:
                    conn.executemany("INSERT INTO nodes VALUES (?,?,?)", node_rows)
                    node_rows.clear()
            return v

        with open(ways_jsonseq, "r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                feat = json.loads(raw)
                props = feat.get("properties") or {}
                geom = feat.get("geometry") or {}
                coords = geom.get("coordinates") or []
                if geom.get("type") != "LineString" or len(coords) < 2:
                    continue

                tags = {k: str(v) for k, v in props.items() if v is not None}
                way_id = int(tags.get("@id", "0").split("/")[-1]) if tags.get("@id") else 0
                drivable = 1 if is_drivable(tags, cfg) else 0
                oneway = parse_oneway(tags)

                for seq in range(len(coords) - 1):
                    lon1, lat1 = coords[seq][0], coords[seq][1]
                    lon2, lat2 = coords[seq + 1][0], coords[seq + 1][1]
                    if lat1 == lat2 and lon1 == lon2:
                        continue
                    n1 = node_id(lat1, lon1)
                    n2 = node_id(lat2, lon2)
                    length = haversine(lat1, lon1, lat2, lon2)
                    min_lat, max_lat = sorted((lat1, lat2))
                    min_lon, max_lon = sorted((lon1, lon2))
                    row = (
                        next_edge_id, way_id, seq, n1, n2, length,
                        tags.get("highway"), tags.get("name"), tags.get("ref"), tags.get("maxspeed"),
                        oneway, drivable, tags.get("access"), tags.get("motor_vehicle"), tags.get("motorcar"),
                        tags.get("bridge"), tags.get("tunnel"), tags.get("layer"),
                        min_lat, max_lat, min_lon, max_lon
                    )
                    edge_rows.append(row)
                    rtree_rows.append((next_edge_id, min_lon, max_lon, min_lat, max_lat))
                    next_edge_id += 1

                    if len(edge_rows) >= 50000:
                        conn.executemany(
                            "INSERT INTO edges VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            edge_rows,
                        )
                        conn.executemany("INSERT INTO edge_rtree VALUES (?,?,?,?,?)", rtree_rows)
                        edge_rows.clear()
                        rtree_rows.clear()

        if node_rows:
            conn.executemany("INSERT INTO nodes VALUES (?,?,?)", node_rows)
        if edge_rows:
            conn.executemany(
                "INSERT INTO edges VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                edge_rows,
            )
            conn.executemany("INSERT INTO edge_rtree VALUES (?,?,?,?,?)", rtree_rows)

    conn.execute("INSERT INTO metadata VALUES (?,?)", ("schemaVersion", "2"))
    conn.execute("INSERT INTO metadata VALUES (?,?)", ("graphType", "segment-graph"))
    conn.execute("INSERT INTO metadata VALUES (?,?)", ("coordinatePrecision", "1e-7 degrees"))
    conn.execute("INSERT INTO metadata VALUES (?,?)", ("turnRestrictions", "not-yet-applied"))
    conn.execute("ANALYZE")
    conn.commit()

    stats = {
        "nodes": conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0],
        "edges": conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0],
        "drivableEdges": conn.execute("SELECT COUNT(*) FROM edges WHERE drivable=1").fetchone()[0],
        "nonDrivableEdges": conn.execute("SELECT COUNT(*) FROM edges WHERE drivable=0").fetchone()[0],
    }
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
