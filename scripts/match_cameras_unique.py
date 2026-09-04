#!/usr/bin/env python3
"""Phase 3 entrypoint with collision-safe camera IDs and schema-safe rows.

The public snapshot can contain multiple non-identical records sharing provider code,
camera number, coordinate and enforcement code. A full canonical-record hash avoids
silently dropping those records while remaining deterministic for an unchanged row.

The base matcher historically emitted one extra NULL for unmatched cameras. Normalize
that legacy error row to the current 46-column camera schema so nationwide builds do not
fail only when the first unmatched camera is encountered.
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


_original_camera_row = match_cameras.camera_row


def schema_safe_camera_row(record, match, error):
    row = _original_camera_row(record, match, error)
    if error and len(row) == 47:
        # Common source fields occupy 23 columns. The unmatched tail should then
        # contain 18 NULL road fields followed by confidence/status/count/gap/reason.
        # The legacy base matcher emits 19 NULLs, so remove the final extra NULL.
        if row[41] is None and row[42] == 0.0:
            del row[41]
    if len(row) != 46:
        raise RuntimeError(
            f"camera row width mismatch after normalization: {len(row)} (error={error!r})"
        )
    return row


match_cameras.stable_camera_id = canonical_camera_id
match_cameras.camera_row = schema_safe_camera_row

if __name__ == "__main__":
    raise SystemExit(match_cameras.main())
