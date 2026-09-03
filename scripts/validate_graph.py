#!/usr/bin/env python3
import json
import sqlite3
import sys
from pathlib import Path


def main():
    if len(sys.argv) != 2:
        print("usage: validate_graph.py camera-ninja-roads.db", file=sys.stderr)
        return 2
    db = Path(sys.argv[1])
    conn = sqlite3.connect(db)
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise SystemExit(f"integrity_check failed: {integrity}")

    nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    rtree = conn.execute("SELECT COUNT(*) FROM edge_rtree").fetchone()[0]
    drivable = conn.execute("SELECT COUNT(*) FROM edges WHERE drivable=1").fetchone()[0]
    bad_lengths = conn.execute("SELECT COUNT(*) FROM edges WHERE length_m <= 0").fetchone()[0]
    bad_boxes = conn.execute("SELECT COUNT(*) FROM edges WHERE min_lat > max_lat OR min_lon > max_lon").fetchone()[0]

    if nodes <= 0 or edges <= 0:
        raise SystemExit("graph is empty")
    if edges != rtree:
        raise SystemExit(f"R-tree row mismatch: edges={edges}, rtree={rtree}")
    if bad_lengths or bad_boxes:
        raise SystemExit(f"invalid geometry rows: bad_lengths={bad_lengths}, bad_boxes={bad_boxes}")

    # Seoul City Hall area sanity query: prove R-tree can return nearby candidates.
    lon, lat, delta = 126.9780, 37.5665, 0.003
    nearby = conn.execute(
        """
        SELECT COUNT(*)
        FROM edge_rtree r
        JOIN edges e ON e.edge_id=r.edge_id
        WHERE r.min_lon <= ? AND r.max_lon >= ?
          AND r.min_lat <= ? AND r.max_lat >= ?
          AND e.drivable=1
        """,
        (lon + delta, lon - delta, lat + delta, lat - delta),
    ).fetchone()[0]
    if nearby <= 0:
        raise SystemExit("R-tree sanity query returned no drivable road candidates")

    result = {
        "integrity": integrity,
        "nodes": nodes,
        "edges": edges,
        "rtreeRows": rtree,
        "drivableEdges": drivable,
        "nonDrivableEdges": edges - drivable,
        "seoulSanityCandidates": nearby,
        "databaseBytes": db.stat().st_size,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
