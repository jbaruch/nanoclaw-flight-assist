"""Tests for nightly-travel-sync/precheck.py.

Locks down the cadence-gate contract per `coding-policy: testing-standards`:

  - travel-db.json missing → wake (reason `no_travel_db`)
  - mtime within the cadence, at the current schema → skip (`within_cadence`)
  - mtime at/over the cadence → wake (reason `cadence_elapsed`)
  - a DB just under the daily cron multiple still wakes (the #803
    near-miss regression the 20h cap — not 24h — exists to prevent)
  - mtime in the future → wake (reason `db_mtime_future`)
  - a fresh DB below the builder's schema → wake (`db_schema_stale`, #268)
  - the mirrored EXPECTED_DB_SCHEMA_VERSION tracks the builder's own
    SCHEMA_VERSION
  - main() emits exactly one line of valid JSON and exits 0
  - main() fails OPEN (wake) on an unexpected internal error

`decide()` takes `now_utc` as an argument, so age math is deterministic
without freezing the clock — tests pass a fixed instant and set the
file's mtime via `os.utime` relative to it.
"""

import json
import os
from datetime import datetime, timedelta, timezone

_NOW = datetime(2026, 5, 31, 6, 0, 0, tzinfo=timezone.utc)


def _write_db(path, schema_version: int | None = 3):
    """Write a travel-db.json carrying `schema_version`. Age tests need a
    DB at the current schema so the schema gate stays out of the way; the
    schema tests vary it."""
    body = {"generated_at": "2026-05-30T06:00:00Z", "trips": {}}
    if schema_version is not None:
        body["schema_version"] = schema_version
    path.write_text(json.dumps(body))


def _set_age(path, days_ago):
    target = (_NOW - timedelta(days=days_ago)).timestamp()
    os.utime(str(path), (target, target))


def _set_age_hours(path, hours_ago):
    target = (_NOW - timedelta(hours=hours_ago)).timestamp()
    os.utime(str(path), (target, target))


def test_missing_db_wakes(nightly_travel_sync_precheck):
    module, db_path = nightly_travel_sync_precheck
    assert not db_path.exists()
    payload = module.decide(_NOW, db_path)
    assert payload["wake_agent"] is True
    assert payload["data"]["reason"] == "no_travel_db"
    assert payload["data"]["path"] == str(db_path)


def test_fresh_db_skips(nightly_travel_sync_precheck):
    """A DB rebuilt a few hours ago is well within the daily cadence."""
    module, db_path = nightly_travel_sync_precheck
    _write_db(db_path)
    _set_age_hours(db_path, hours_ago=6)
    payload = module.decide(_NOW, db_path)
    assert payload["wake_agent"] is False
    assert payload["data"]["reason"] == "within_cadence"


def test_day_old_db_wakes(nightly_travel_sync_precheck):
    """#268: a day-old DB is stale now. `morning-brief` reads travel-db.json
    daily, so the refresh has to land ahead of each brief."""
    module, db_path = nightly_travel_sync_precheck
    _write_db(db_path)
    _set_age(db_path, days_ago=1)
    payload = module.decide(_NOW, db_path)
    assert payload["wake_agent"] is True
    assert payload["data"]["reason"] == "cadence_elapsed"


def test_just_under_cadence_skips(nightly_travel_sync_precheck):
    """19h < CADENCE (20h) → still within."""
    module, db_path = nightly_travel_sync_precheck
    _write_db(db_path)
    _set_age_hours(db_path, hours_ago=19)
    payload = module.decide(_NOW, db_path)
    assert payload["wake_agent"] is False
    assert payload["data"]["reason"] == "within_cadence"


def test_at_cadence_boundary_wakes(nightly_travel_sync_precheck):
    """Boundary: age >= CADENCE (20h) wakes."""
    module, db_path = nightly_travel_sync_precheck
    _write_db(db_path)
    _set_age_hours(db_path, hours_ago=20)
    payload = module.decide(_NOW, db_path)
    assert payload["wake_agent"] is True
    assert payload["data"]["reason"] == "cadence_elapsed"


def test_near_miss_just_under_daily_multiple_wakes(nightly_travel_sync_precheck):
    """#803 regression: a DB stamped just under the daily (24h) cron multiple
    MUST wake. With an exact-24h cap the DB — stamped at run completion, so
    ~23.9h old at the next daily fire — reads as < 24h and skips, slipping the
    run to every other day. The 20h cap sits below the multiple, so 23.9h ≥ 20h
    wakes."""
    module, db_path = nightly_travel_sync_precheck
    _write_db(db_path)
    _set_age_hours(db_path, hours_ago=24 - (5 / 60))  # 1 day minus 5 minutes
    payload = module.decide(_NOW, db_path)
    assert payload["wake_agent"] is True
    assert payload["data"]["reason"] == "cadence_elapsed"


def test_far_stale_db_wakes(nightly_travel_sync_precheck):
    module, db_path = nightly_travel_sync_precheck
    _write_db(db_path)
    _set_age(db_path, days_ago=30)
    payload = module.decide(_NOW, db_path)
    assert payload["wake_agent"] is True
    assert payload["data"]["reason"] == "cadence_elapsed"


def test_future_mtime_wakes(nightly_travel_sync_precheck):
    """A DB stamped in the future (clock skew / bad write) wakes so the
    next run rewrites it."""
    module, db_path = nightly_travel_sync_precheck
    _write_db(db_path)
    _set_age(db_path, days_ago=-1)
    payload = module.decide(_NOW, db_path)
    assert payload["wake_agent"] is True
    assert payload["data"]["reason"] == "db_mtime_future"


def test_fresh_db_below_builder_schema_wakes(nightly_travel_sync_precheck):
    """#268: the DB is fresh by mtime and stale by content. Before the schema
    gate this skipped, leaving a shipped schema-bumping fix inert until the DB
    aged past the cadence cap."""
    module, db_path = nightly_travel_sync_precheck
    _write_db(db_path, schema_version=module.EXPECTED_DB_SCHEMA_VERSION - 1)
    _set_age_hours(db_path, hours_ago=1)
    payload = module.decide(_NOW, db_path)
    assert payload["wake_agent"] is True
    assert payload["data"]["reason"] == "db_schema_stale"
    assert payload["data"]["db_schema_version"] == module.EXPECTED_DB_SCHEMA_VERSION - 1
    assert payload["data"]["expected_schema_version"] == module.EXPECTED_DB_SCHEMA_VERSION


def test_fresh_db_at_builder_schema_skips(nightly_travel_sync_precheck):
    module, db_path = nightly_travel_sync_precheck
    _write_db(db_path, schema_version=module.EXPECTED_DB_SCHEMA_VERSION)
    _set_age_hours(db_path, hours_ago=1)
    payload = module.decide(_NOW, db_path)
    assert payload["wake_agent"] is False
    assert payload["data"]["reason"] == "within_cadence"


def test_fresh_db_above_builder_schema_skips(nightly_travel_sync_precheck):
    """A DB stamped ahead of this precheck means the precheck is the lagging
    side. Waking would only drive the builder into its refuse-to-downgrade
    guard, so the age cap alone governs."""
    module, db_path = nightly_travel_sync_precheck
    _write_db(db_path, schema_version=module.EXPECTED_DB_SCHEMA_VERSION + 1)
    _set_age_hours(db_path, hours_ago=1)
    payload = module.decide(_NOW, db_path)
    assert payload["wake_agent"] is False
    assert payload["data"]["reason"] == "within_cadence"


def test_fresh_db_without_schema_stamp_wakes(nightly_travel_sync_precheck):
    """Unstamped legacy data is implicit v1 per check-travel-bookings'
    state-schema.md — below the builder, so rebuild."""
    module, db_path = nightly_travel_sync_precheck
    _write_db(db_path, schema_version=None)
    _set_age_hours(db_path, hours_ago=1)
    payload = module.decide(_NOW, db_path)
    assert payload["wake_agent"] is True
    assert payload["data"]["reason"] == "db_schema_stale"
    assert payload["data"]["db_schema_version"] is None


def test_fresh_but_malformed_db_wakes(nightly_travel_sync_precheck):
    """A DB that will not parse cannot be at the current schema. Wake and let
    Step 4 rewrite it rather than skipping on a healthy-looking mtime."""
    module, db_path = nightly_travel_sync_precheck
    db_path.write_text("{not json")
    _set_age_hours(db_path, hours_ago=1)
    payload = module.decide(_NOW, db_path)
    assert payload["wake_agent"] is True
    assert payload["data"]["reason"] == "db_schema_stale"
    assert payload["data"]["db_schema_version"] is None


def test_fresh_non_object_db_wakes(nightly_travel_sync_precheck):
    """travel-db.json is a top-level object. A bare array is not it."""
    module, db_path = nightly_travel_sync_precheck
    db_path.write_text("[]")
    _set_age_hours(db_path, hours_ago=1)
    payload = module.decide(_NOW, db_path)
    assert payload["wake_agent"] is True
    assert payload["data"]["reason"] == "db_schema_stale"


def test_boolean_schema_stamp_is_not_a_version(nightly_travel_sync_precheck):
    """`True` is an int in Python. It is not a schema version."""
    module, db_path = nightly_travel_sync_precheck
    _write_db(db_path, schema_version=True)
    _set_age_hours(db_path, hours_ago=1)
    payload = module.decide(_NOW, db_path)
    assert payload["wake_agent"] is True
    assert payload["data"]["reason"] == "db_schema_stale"
    assert payload["data"]["db_schema_version"] is None


def test_expected_schema_mirrors_the_builder(nightly_travel_sync_precheck, build_travel_db):
    """The precheck mirrors the builder's SCHEMA_VERSION rather than importing
    it (the builder's plugin mount is not on the host-side path). This test is
    what keeps the mirror honest — bump one, bump the other."""
    module, _ = nightly_travel_sync_precheck
    builder = build_travel_db[0]
    assert module.EXPECTED_DB_SCHEMA_VERSION == builder.SCHEMA_VERSION


def test_stale_by_age_and_schema_reports_schema(nightly_travel_sync_precheck):
    """Both gates trip — the schema is the more actionable reason, so it wins."""
    module, db_path = nightly_travel_sync_precheck
    _write_db(db_path, schema_version=module.EXPECTED_DB_SCHEMA_VERSION - 1)
    _set_age(db_path, days_ago=30)
    payload = module.decide(_NOW, db_path)
    assert payload["wake_agent"] is True
    assert payload["data"]["reason"] == "db_schema_stale"


def test_future_mtime_beats_schema_check(nightly_travel_sync_precheck):
    """A future stamp is a clock/write fault — diagnose that first, whatever
    the schema says."""
    module, db_path = nightly_travel_sync_precheck
    _write_db(db_path, schema_version=module.EXPECTED_DB_SCHEMA_VERSION - 1)
    _set_age(db_path, days_ago=-1)
    payload = module.decide(_NOW, db_path)
    assert payload["wake_agent"] is True
    assert payload["data"]["reason"] == "db_mtime_future"


def test_main_emits_single_line_json_and_exits_zero(
    nightly_travel_sync_precheck, monkeypatch, capsys
):
    module, db_path = nightly_travel_sync_precheck
    monkeypatch.setenv("NIGHTLY_TRAVEL_SYNC_DB", str(db_path))  # missing → wake
    code = module.main()
    assert code == 0
    out = capsys.readouterr().out
    lines = [ln for ln in out.split("\n") if ln]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["wake_agent"] is True
    assert payload["data"]["reason"] == "no_travel_db"


def test_main_fails_open_on_internal_error(nightly_travel_sync_precheck, monkeypatch, capsys):
    """An unexpected exception inside main() must emit the safe-shape
    wake payload (fail OPEN) and exit 0 — never crash the precheck."""
    module, db_path = nightly_travel_sync_precheck

    def _boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(module, "decide", _boom)
    monkeypatch.setenv("NIGHTLY_TRAVEL_SYNC_DB", str(db_path))
    code = module.main()
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"wake_agent": True, "data": {"reason": "precheck_internal_error"}}
