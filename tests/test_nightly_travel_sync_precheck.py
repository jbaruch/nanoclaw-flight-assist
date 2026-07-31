"""Tests for nightly-travel-sync/precheck.py.

Locks down the cadence-gate contract per `coding-policy: testing-standards`:

  - travel-db.json missing → wake (reason `no_travel_db`)
  - mtime within the 60h cadence → skip (reason `within_cadence`)
  - mtime at/over the 60h cadence → wake (reason `cadence_elapsed`)
  - a cursor just under the 3-day cron multiple still wakes (the #803
    near-miss regression the 60h cap — not 72h — exists to prevent)
  - mtime in the future → wake (reason `db_mtime_future`)
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
    """One-day-old DB is well within the 3-day cadence."""
    module, db_path = nightly_travel_sync_precheck
    db_path.write_text("{}")
    _set_age(db_path, days_ago=1)
    payload = module.decide(_NOW, db_path)
    assert payload["wake_agent"] is False
    assert payload["data"]["reason"] == "within_cadence"


def test_two_day_old_db_still_within_cadence(nightly_travel_sync_precheck):
    module, db_path = nightly_travel_sync_precheck
    db_path.write_text("{}")
    _set_age(db_path, days_ago=2)
    payload = module.decide(_NOW, db_path)
    assert payload["wake_agent"] is False
    assert payload["data"]["reason"] == "within_cadence"


def test_just_under_cadence_skips(nightly_travel_sync_precheck):
    """59h < CADENCE (60h) → still within."""
    module, db_path = nightly_travel_sync_precheck
    db_path.write_text("{}")
    _set_age_hours(db_path, hours_ago=59)
    payload = module.decide(_NOW, db_path)
    assert payload["wake_agent"] is False
    assert payload["data"]["reason"] == "within_cadence"


def test_at_cadence_boundary_wakes(nightly_travel_sync_precheck):
    """Boundary: age >= CADENCE (60h) wakes."""
    module, db_path = nightly_travel_sync_precheck
    db_path.write_text("{}")
    _set_age_hours(db_path, hours_ago=60)
    payload = module.decide(_NOW, db_path)
    assert payload["wake_agent"] is True
    assert payload["data"]["reason"] == "cadence_elapsed"


def test_near_miss_just_under_three_day_multiple_wakes(nightly_travel_sync_precheck):
    """#803 regression: a cursor stamped just under the 3-day (72h) cron multiple
    MUST wake. With the old exact-3-day cap the cursor — stamped at run completion,
    so ~71.9h old at the third daily fire — read as < 72h and skipped forever,
    slipping the run by a whole period. The 60h cap sits below the multiple, so
    71.9h ≥ 60h wakes."""
    module, db_path = nightly_travel_sync_precheck
    db_path.write_text("{}")
    _set_age_hours(db_path, hours_ago=72 - (5 / 60))  # 3 days minus 5 minutes
    payload = module.decide(_NOW, db_path)
    assert payload["wake_agent"] is True
    assert payload["data"]["reason"] == "cadence_elapsed"


def test_far_stale_db_wakes(nightly_travel_sync_precheck):
    module, db_path = nightly_travel_sync_precheck
    db_path.write_text("{}")
    _set_age(db_path, days_ago=30)
    payload = module.decide(_NOW, db_path)
    assert payload["wake_agent"] is True
    assert payload["data"]["reason"] == "cadence_elapsed"


def test_future_mtime_wakes(nightly_travel_sync_precheck):
    """A DB stamped in the future (clock skew / bad write) wakes so the
    next run rewrites it."""
    module, db_path = nightly_travel_sync_precheck
    db_path.write_text("{}")
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
