#!/usr/bin/env python3
"""Phase 3 entrypoint with collision-safe camera IDs.

The public snapshot can contain multiple non-identical records sharing provider code,
camera number, coordinate and enforcement code. A full canonical-record hash avoids
silently dropping those records while remaining deterministic for an unchanged row.
"""
import hashlib
import json

import match_cameras


def canonical_camera_id(record):
    canonical = json.dumps(
        record,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()


match_cameras.stable_camera_id = canonical_camera_id

if __name__ == "__main__":
    raise SystemExit(match_cameras.main())
