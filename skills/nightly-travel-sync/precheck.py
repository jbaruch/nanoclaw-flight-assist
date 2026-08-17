#!/usr/bin/env python3
"""Cadence precheck for `tessl__nightly-travel-sync`.

Fires daily via the cadence-registry (`0 6 * * * (TZ=local)`). Gates the
wake on a filesystem cadence cap plus a schema-currency check, both
anchored on the bundle's terminal artifact,
`/workspace/group/travel-db.json` — the file Step 4 rebuilds last and the
one downstream consumers (`check-travel-bookings`, `morning-brief`)
actually read.

Anchoring on travel-db.json rather than travel-schedule.json is
deliberate: travel-schedule.json (Step 2's output) bumps on every
successful ICS refresh even when a later step fails, which would reset
the cadence while the DB stayed stale. travel-db.json bumps only after
the refresh → build pipeline reaches Step 4, so its mtime is the honest
"the pipeline produced its output" signal — the same semantics the admin
bundle's end-of-run cursor stamp carried before this extract. No
separate cursor file is owned, so the gate adds no self-owned state per
`jbaruch/nanoclaw-admin#318`.

Wake conditions:
  - travel-db.json missing (cold start, or pruned) — wake.
  - mtime in the future (clock skew / bad write) — wake so the next run
    rewrites it.
  - on-disk schema_version below the builder's — wake, so a shipped
    schema bump activates on the next fire instead of idling until the
    DB happens to age past the cadence cap (#268).
  - travel-db.json mtime older than CADENCE — wake.
  - fresh and at the current schema — skip silently.

Age alone is a schema-blind signal: a DB written before a
schema-bumping fix shipped is "fresh" by mtime and stale by content, and
the fix stays inert until the cap elapses. #267's traveller-local trip
dates landed that way — merged 08-12, still not in effect on 08-13's
brief, and not due to rebuild until 08-14. The schema check closes that
window; the cadence cap keeps handling ordinary staleness.

Scheduled-task contract: emits single-line JSON `{"wake_agent": <bool>,
"data": {...}}` on stdout, exit 0 always (per agent-runner contract — a
non-zero exit or invalid stdout is read as wake_agent=false, which would
silently freeze the travel-data refresh). The sole catch-all sits inside
`main()` so the outer-boundary-process-contract carve-out's "outermost
process boundary" precondition holds; it fails OPEN (wake) so a transient
stat error can't freeze the pipeline for days.

stdlib-only per `jbaruch/coding-policy: dependency-management`.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 20h — a daily refresh, deliberately NOT the exact 24h multiple of the daily
# cron. The DB stamps at run completion, so an exact-multiple cap near-misses:
# the next daily fire finds it ~23.9h old (< 24h) and skips, slipping the run to
# every other day (jbaruch/nanoclaw#803, which the earlier 60h-under-72h cap
# encoded the same way). 20h sits a comfortable margin under the multiple, with
# slack for run latency and DST — see nanoclaw-host:
# rules/overlay-tile-authoring.md. `morning-brief` reads travel-db.json daily,
# so anything longer lets a booked hotel get nagged for days (#268).
CADENCE = timedelta(hours=20)

# The schema `check-travel-bookings/scripts/build-travel-db.py` emits. Mirrored
# rather than imported: this precheck runs host-side on the cadence-registry,
# where the builder's plugin mount is not on the path, and the module is
# stdlib-only by contract. `tests/test_nightly_travel_sync_precheck.py` asserts
# the mirror against the builder's own SCHEMA_VERSION, so drift fails CI.
EXPECTED_DB_SCHEMA_VERSION = 3

DEFAULT_DB_PATH = "/workspace/group/travel-db.json"


def _db_schema_version(db_path: Path) -> int | None:
    """The DB's on-disk `schema_version`, or None when it cannot be read.

    None covers an unreadable, malformed, or non-object DB, and one with
    no `schema_version` stamp (per `check-travel-bookings/state-schema.md`,
    legacy unstamped data is implicit v1). Every one of those means "not
    at the current schema", so `decide()` treats None and a low stamp
    alike: rebuild. This is a non-owner read of a `check-travel-bookings`
    artifact — it never writes and never migrates, per
    `jbaruch/coding-policy: stateful-artifacts`.
    """
    try:
        with db_path.open(encoding="utf-8") as f:
            db = json.load(f)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(db, dict):
        return None
    version = db.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        return None
    return version


def decide(now_utc: datetime, db_path: Path) -> dict:
    if not db_path.exists():
        return {
            "wake_agent": True,
            "data": {"reason": "no_travel_db", "path": str(db_path)},
        }

    mtime = datetime.fromtimestamp(db_path.stat().st_mtime, tz=timezone.utc)
    age = now_utc - mtime
    age_hours = round(age.total_seconds() / 3600.0, 2)

    if age < timedelta(0):
        return {
            "wake_agent": True,
            "data": {
                "reason": "db_mtime_future",
                "mtime": mtime.isoformat(),
                "age_hours": age_hours,
            },
        }

    schema_version = _db_schema_version(db_path)
    if schema_version is None or schema_version < EXPECTED_DB_SCHEMA_VERSION:
        return {
            "wake_agent": True,
            "data": {
                "reason": "db_schema_stale",
                "mtime": mtime.isoformat(),
                "age_hours": age_hours,
                "db_schema_version": schema_version,
                "expected_schema_version": EXPECTED_DB_SCHEMA_VERSION,
            },
        }

    if age >= CADENCE:
        return {
            "wake_agent": True,
            "data": {
                "reason": "cadence_elapsed",
                "mtime": mtime.isoformat(),
                "age_hours": age_hours,
                "cadence_hours": CADENCE.total_seconds() / 3600.0,
            },
        }

    return {
        "wake_agent": False,
        "data": {
            "reason": "within_cadence",
            "mtime": mtime.isoformat(),
            "age_hours": age_hours,
            "cadence_hours": CADENCE.total_seconds() / 3600.0,
        },
    }


def main() -> int:
    # outer-boundary-process-contract: the agent-runner reads non-zero
    # exit OR invalid stdout JSON as wake_agent=false, which here would
    # silently freeze the travel-data refresh pipeline. Every unexpected
    # exception flows into a safe-shape JSON payload + exit 0 so the
    # contract stays honest. This handler fails OPEN (wake_agent=true) —
    # a transient stat error must not pin the pipeline closed for days;
    # the bundle is idempotent, so an extra wake is cheap. See
    # `jbaruch/coding-policy: error-handling`. Sole catch-all in the file.
    try:
        db_path = Path(os.environ.get("NIGHTLY_TRAVEL_SYNC_DB", DEFAULT_DB_PATH))
        now = datetime.now(timezone.utc)
        payload = decide(now, db_path)
    except Exception:  # noqa: BLE001 — outer-boundary-process-contract
        traceback.print_exc(file=sys.stderr)
        payload = {"wake_agent": True, "data": {"reason": "precheck_internal_error"}}
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
