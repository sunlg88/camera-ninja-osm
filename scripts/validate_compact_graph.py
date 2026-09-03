#!/usr/bin/env python3
import json
import sqlite3
import sys
from pathlib import Path


def main():
    if len(sys.argv) != 2:
        print("usage: validate_compact_graph.py compact.db", file=sys.stderr)
        return 2

    db = Path(sys.argv[1])
    conn = sqlite3.connect(db)

    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    ways = conn.execute("SELECT COUNT(*) FROM ways").fetchone()[0]
    segments = conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
    rtree = conn.execute("SELECT COUNT(*) FROM segment_rtree").fetchone()[0]
    bad_geom = conn.execute("SELECT COUNT(*) FROM segments WHERE length(geometry) < 5 OR point_count < 2 OR length_m <= 0").fetchone()[0]
    orphan_way = conn.execute("SELECT COUNT(*) FROM segments s LEFT JOIN ways w ON w.way_id=s.way_id WHERE w.way_id IS NULL").fetchone()[0]

    # Seoul City Hall area: spatial index must return plausible nearby candidates.
    lon, lat = 126.9779, 37.5663
    d = 0.003
    seoul = conn.execute(
        """
        SELECT COUNT(*)
        FROM segment_rtree r
        JOIN segments s ON s.segment_id=r.segment_id
        WHERE r.min_lon <= ? AND r.max_lon >= ?
          AND r.min_lat <= ? AND r.max_lat >= ?
        """,
        (lon + d, lon - d, lat + d, lat - d),
    ).fetchone()[0]

    metadata = dict(conn.execute("SELECT key,value FROM metadata"))
    result = {
        "integrity": integrity,
        "ways": ways,
        "segments": segments,
        "rtreeRows": rtree,
        "badGeometryRows": bad_geom,
        "orphanSegmentWays": orphan_way,
        "seoulSanityCandidates": seoul,
        "databaseBytes": db.stat().st_size,
        "metadata": metadata,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))

    ok = (
        integrity == "ok"
        and ways > 500000
        and segments > 500000
        and segments == rtree
        and bad_geom == 0
        and orphan_way == 0
        and seoul > 0
    )
    conn.close()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
