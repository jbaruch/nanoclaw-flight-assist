"""Baseline tests for skills/check-travel-bookings/scripts/build-travel-db.py.

Locks down the documented contract per `coding-policy: testing-standards`:

  - Reads `travel-schedule.json` (a flat event list); writes
    `travel-db.json` with a `{schema_version, generated_at, trips: {<slug>: {...}}}`
    shape per the sibling `state-schema.md`
  - Trips (events without `item-` in `uid`) are kept iff their `end`
    is on/after today; items overlap into the trip's days bucket
  - Each day's items are sorted by `TYPE_ORDER` (Flight, Rail,
    Lodging, Car Rental, then alphabetic)
  - `schema_version` is stamped on every write, matching the module
    constant; existing forward-versioned DBs are not overwritten
  - Exit 1 on missing schedule (with stderr diagnostic naming the
    expected path); exit 1 on unreadable / invalid-JSON / wrong-root-shape
    schedule (stderr names the rewrite path); exit 2 on attempted
    forward-schema downgrade
"""

import json
from datetime import date

_FROZEN_TODAY = date(2026, 4, 30)


def _make_frozen_date(real_date):
    class FrozenDate(real_date):
        @classmethod
        def today(cls):
            return _FROZEN_TODAY

    return FrozenDate


def _run(module, monkeypatch, capsys, freeze=True):
    monkeypatch.setattr("sys.argv", ["build-travel-db.py"])
    if freeze:
        monkeypatch.setattr(module, "date", _make_frozen_date(date))
    code = 0
    try:
        module.main()
    except SystemExit as exc:
        code = 0 if exc.code is None else int(exc.code)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _trip(uid, summary, start, end):
    return {
        "uid": uid,
        "summary": summary,
        "start": start,
        "end": end,
        "type": "Trip",
    }


def _item(uid, summary, start, end, item_type):
    return {
        "uid": uid,
        "summary": summary,
        "start": start,
        "end": end,
        "type": item_type,
    }


def test_missing_schedule_exits_1(build_travel_db, monkeypatch, capsys):
    module, schedule_path, _ = build_travel_db
    assert not schedule_path.exists()
    code, _, err = _run(module, monkeypatch, capsys)
    assert code == 1
    assert str(schedule_path) in err
    assert "run refresh-travel-schedule.py first" in err


def test_invalid_json_exits_1_with_diagnostic(build_travel_db, monkeypatch, capsys):
    """A partially-written or corrupt schedule (truncated JSON) exits 1
    with an actionable stderr message instead of a raw traceback."""
    module, schedule_path, db_path = build_travel_db
    schedule_path.write_text('[{"uid": "trip-1", "summary": "Trunc')
    code, _, err = _run(module, monkeypatch, capsys)
    assert code == 1
    assert str(schedule_path) in err
    assert "re-run refresh-travel-schedule.py" in err
    assert not db_path.exists()


def test_non_utf8_schedule_exits_1_with_diagnostic(build_travel_db, monkeypatch, capsys):
    """Non-UTF-8 bytes in the schedule exit 1 with the rewrite
    diagnostic, not a UnicodeDecodeError traceback."""
    module, schedule_path, db_path = build_travel_db
    schedule_path.write_bytes(b"\xff\xfe[]")
    code, _, err = _run(module, monkeypatch, capsys)
    assert code == 1
    assert "re-run refresh-travel-schedule.py" in err
    assert not db_path.exists()


def test_wrong_root_shape_exits_1_with_diagnostic(build_travel_db, monkeypatch, capsys):
    """Valid JSON with the wrong root shape (object instead of the
    documented event array, or an array of non-objects) exits 1 with
    the contract named in stderr."""
    module, schedule_path, db_path = build_travel_db
    for bad_root in ({"trips": []}, ["not-an-event", 42]):
        schedule_path.write_text(json.dumps(bad_root))
        code, _, err = _run(module, monkeypatch, capsys)
        assert code == 1
        assert "root must be a JSON array of event objects" in err
        assert not db_path.exists()


def test_writes_db_with_trips_and_items(build_travel_db, monkeypatch, capsys):
    module, schedule_path, db_path = build_travel_db
    schedule = [
        _trip("trip-1", "Boston Conf", "2026-05-10", "2026-05-13"),
        _item("item-1", "ATL→BOS", "2026-05-10", "2026-05-10", "Flight"),
        _item("item-2", "Hilton Boston", "2026-05-10", "2026-05-13", "Lodging"),
    ]
    schedule_path.write_text(json.dumps(schedule))
    code, _, _ = _run(module, monkeypatch, capsys)
    assert code == 0
    db = json.loads(db_path.read_text())
    assert "generated_at" in db
    assert len(db["trips"]) == 1
    slug = next(iter(db["trips"]))
    trip = db["trips"][slug]
    assert trip["summary"] == "Boston Conf"
    assert trip["start"] == "2026-05-10"
    # Item lands in its start-day bucket
    assert "2026-05-10" in trip["days"]


def test_past_trip_excluded(build_travel_db, monkeypatch, capsys):
    """Trip ending before frozen-today is dropped."""
    module, schedule_path, db_path = build_travel_db
    schedule = [
        _trip("old-trip", "Old Trip", "2026-04-01", "2026-04-05"),
        _trip("future-trip", "Future Trip", "2026-06-10", "2026-06-12"),
    ]
    schedule_path.write_text(json.dumps(schedule))
    _run(module, monkeypatch, capsys)
    db = json.loads(db_path.read_text())
    assert len(db["trips"]) == 1
    # Assert on the surviving trip's *value* (summary, start, end)
    # rather than the slug string — slug derivation isn't part of the
    # documented contract.
    surviving = next(iter(db["trips"].values()))
    assert surviving["summary"] == "Future Trip"
    assert surviving["start"] == "2026-06-10"
    assert surviving["end"] == "2026-06-12"


def test_items_sorted_by_type_within_day(build_travel_db, monkeypatch, capsys):
    """Multiple items on the same day sort: Flight < Rail < Lodging < Car Rental."""
    module, schedule_path, db_path = build_travel_db
    schedule = [
        _trip("trip", "Multiday", "2026-05-10", "2026-05-13"),
        _item("item-c", "Hertz", "2026-05-10", "2026-05-10", "Car Rental"),
        _item("item-l", "Hotel", "2026-05-10", "2026-05-12", "Lodging"),
        _item("item-f", "Flight 1", "2026-05-10", "2026-05-10", "Flight"),
    ]
    schedule_path.write_text(json.dumps(schedule))
    _run(module, monkeypatch, capsys)
    db = json.loads(db_path.read_text())
    slug = next(iter(db["trips"]))
    day = db["trips"][slug]["days"]["2026-05-10"]
    types = [e["type"] for e in day]
    assert types == ["Flight", "Lodging", "Car Rental"]


def test_overlapping_items_distributed_to_start_day(build_travel_db, monkeypatch, capsys):
    """Items get bucketed by their `start` date, even when they span days."""
    module, schedule_path, db_path = build_travel_db
    schedule = [
        _trip("trip", "Trip", "2026-05-10", "2026-05-15"),
        _item("item-l", "Hotel", "2026-05-11", "2026-05-14", "Lodging"),
    ]
    schedule_path.write_text(json.dumps(schedule))
    _run(module, monkeypatch, capsys)
    db = json.loads(db_path.read_text())
    slug = next(iter(db["trips"]))
    days = db["trips"][slug]["days"]
    assert "2026-05-11" in days  # bucketed at start date
    assert "2026-05-12" not in days


def test_unknown_type_kept_with_default_order(build_travel_db, monkeypatch, capsys):
    """A novel item type (TripIt adds a new category) sorts after the
    known types per the `TYPE_ORDER` dict's default of 9."""
    module, schedule_path, db_path = build_travel_db
    schedule = [
        _trip("trip", "Trip", "2026-05-10", "2026-05-12"),
        _item("item-novel", "ZebraBoat", "2026-05-10", "2026-05-10", "Boat"),
        _item("item-flight", "Flight", "2026-05-10", "2026-05-10", "Flight"),
    ]
    schedule_path.write_text(json.dumps(schedule))
    _run(module, monkeypatch, capsys)
    db = json.loads(db_path.read_text())
    slug = next(iter(db["trips"]))
    types = [e["type"] for e in db["trips"][slug]["days"]["2026-05-10"]]
    # Flight (order 0) before Boat (default order 9)
    assert types == ["Flight", "Boat"]


def test_refuses_to_overwrite_forward_schema(build_travel_db, monkeypatch, capsys):
    """Per state-schema.md migration policy, the writer must NOT
    overwrite a `travel-db.json` already stamped with a higher
    schema_version than the writer's constant. Otherwise an older
    writer (post-rollback or post-downgrade) would clobber forward-
    migrated state. Exit code 2 + stderr diagnostic name the upgrade
    path so operators can recover."""
    module, schedule_path, db_path = build_travel_db
    schedule_path.write_text(json.dumps([_trip("trip-1", "Lisbon", "2026-06-01", "2026-06-03")]))
    db_path.write_text(json.dumps({"schema_version": 99, "trips": {}}))
    pre_write = db_path.read_text()
    code, _, err = _run(module, monkeypatch, capsys)
    assert code == 2
    assert "schema_version=99" in err
    assert "refusing to downgrade" in err
    # DB on disk is unchanged
    assert db_path.read_text() == pre_write


def test_stdout_is_structured_json(build_travel_db, monkeypatch, capsys):
    """Per `coding-policy: script-delegation`, scripts emit structured
    JSON on stdout — not prose. The success-case stdout parses as a
    single JSON object with `schema_version`, `trips_written`,
    `item_events_written`, and a `trips` summary list."""
    module, schedule_path, db_path = build_travel_db
    schedule = [
        _trip("trip-1", "Vienna", "2026-06-10", "2026-06-13"),
        _item("item-1", "Outbound", "2026-06-10", "2026-06-10", "Flight"),
        _item("item-2", "Hotel", "2026-06-10", "2026-06-13", "Lodging"),
    ]
    schedule_path.write_text(json.dumps(schedule))
    code, out, _ = _run(module, monkeypatch, capsys)
    assert code == 0
    payload = json.loads(out)
    assert payload["schema_version"] == 3
    assert payload["trips_written"] == 1
    assert payload["item_events_written"] == 2
    assert len(payload["trips"]) == 1
    summary = payload["trips"][0]
    assert summary["summary"] == "Vienna"
    assert summary["type_counts"] == {"Flight": 1, "Lodging": 1}


def test_output_stamps_schema_version(build_travel_db, monkeypatch, capsys):
    """Per `coding-policy: stateful-artifacts` + state-schema.md, every
    write of `travel-db.json` carries `schema_version` matching the
    module constant. Locks the contract so a future bump can't silently
    drop the field."""
    module, schedule_path, db_path = build_travel_db
    schedule = [
        _trip("trip-1", "Schema Trip", "2026-06-01", "2026-06-03"),
    ]
    schedule_path.write_text(json.dumps(schedule))
    code, _, _ = _run(module, monkeypatch, capsys)
    assert code == 0
    db = json.loads(db_path.read_text())
    assert db["schema_version"] == module.SCHEMA_VERSION == 3


def test_timed_item_buckets_by_date_and_preserves_time(build_travel_db, monkeypatch, capsys):
    """Timed-item shape from `refresh-travel-schedule.py` post-`nanoclaw-admin#289`
    (`start`/`end` as `YYYY-MM-DDTHH:MM:SSZ`) parses without error,
    buckets under the calendar-date `day_key`, and propagates the full
    ISO-datetime string into the per-day record so downstream consumers
    (flight-assist, the `time_to_leave` precheck) can read the departure
    time without going back to TripIt."""
    module, schedule_path, db_path = build_travel_db
    schedule = [
        _trip("trip", "Munich Trip", "2026-05-21", "2026-05-23"),
        _item(
            "item-flight", "DL23 MUC→DTW", "2026-05-22T07:00:00Z", "2026-05-22T14:00:00Z", "Flight"
        ),
    ]
    schedule_path.write_text(json.dumps(schedule))
    code, _, _ = _run(module, monkeypatch, capsys)
    assert code == 0
    db = json.loads(db_path.read_text())
    slug = next(iter(db["trips"]))
    days = db["trips"][slug]["days"]
    assert "2026-05-22" in days
    flight = days["2026-05-22"][0]
    assert flight["start"] == "2026-05-22T07:00:00Z"
    assert flight["end"] == "2026-05-22T14:00:00Z"


def test_local_stamps_pass_through_and_drive_the_day_key(build_travel_db, monkeypatch, capsys):
    """A v3 schedule record's `start_local`/`end_local` reach the day item
    untouched, and the day key follows the LOCAL date. The red-eye below is
    an 06:05Z May 23 instant that the traveller boarded at 11:05 PM on May
    22 — filing it under the 23rd is what made the night of the 22nd read as
    a night owed a hotel (#268)."""
    module, schedule_path, db_path = build_travel_db
    schedule = [
        _trip("trip", "SF Trip", "2026-05-18", "2026-05-24"),
        {
            **_item(
                "item-red-eye",
                "WN1683 SFO to BNA",
                "2026-05-23T06:05:00Z",
                "2026-05-23T10:30:00Z",
                "Flight",
            ),
            "start_local": "2026-05-22T23:05:00-07:00",
            "end_local": "2026-05-23T05:30:00-05:00",
        },
    ]
    schedule_path.write_text(json.dumps(schedule))
    code, _, _ = _run(module, monkeypatch, capsys)
    assert code == 0
    db = json.loads(db_path.read_text())
    days = db["trips"][next(iter(db["trips"]))]["days"]
    assert "2026-05-22" in days
    assert "2026-05-23" not in days
    flight = days["2026-05-22"][0]
    assert flight["start"] == "2026-05-23T06:05:00Z"
    assert flight["start_local"] == "2026-05-22T23:05:00-07:00"
    assert flight["end_local"] == "2026-05-23T05:30:00-05:00"


def test_item_without_local_stamps_keeps_the_utc_day_key(build_travel_db, monkeypatch, capsys):
    """A v2 schedule record, or a v3 one whose local time never resolved,
    carries no local fields and buckets by its UTC date exactly as before."""
    module, schedule_path, db_path = build_travel_db
    schedule = [
        _trip("trip", "Munich Trip", "2026-05-21", "2026-05-23"),
        _item(
            "item-flight",
            "DL23 MUC to DTW",
            "2026-05-22T07:00:00Z",
            "2026-05-22T14:00:00Z",
            "Flight",
        ),
    ]
    schedule_path.write_text(json.dumps(schedule))
    code, _, _ = _run(module, monkeypatch, capsys)
    assert code == 0
    db = json.loads(db_path.read_text())
    flight = db["trips"][next(iter(db["trips"]))]["days"]["2026-05-22"][0]
    assert "start_local" not in flight
    assert "end_local" not in flight


def test_blank_local_stamp_is_not_carried(build_travel_db, monkeypatch, capsys):
    """An empty-string local stamp is absence, not a date. Copying it would
    make `[:10]` produce an empty day key."""
    module, schedule_path, db_path = build_travel_db
    schedule = [
        _trip("trip", "Munich Trip", "2026-05-21", "2026-05-23"),
        {
            **_item(
                "item-flight",
                "DL23 MUC to DTW",
                "2026-05-22T07:00:00Z",
                "2026-05-22T14:00:00Z",
                "Flight",
            ),
            "start_local": "",
        },
    ]
    schedule_path.write_text(json.dumps(schedule))
    code, _, _ = _run(module, monkeypatch, capsys)
    assert code == 0
    db = json.loads(db_path.read_text())
    days = db["trips"][next(iter(db["trips"]))]["days"]
    assert "2026-05-22" in days
    assert "start_local" not in days["2026-05-22"][0]


# --- v3: trip destination (#271) -------------------------------------------


def test_destination_is_persisted_from_the_schedule(build_travel_db, monkeypatch, capsys):
    """The schedule hands over a decoded location since its v4 (#275). The DB
    carries that readable form — it is what the booking check compares against
    the operator's configured home metro."""
    module, schedule_path, db_path = build_travel_db
    schedule = [
        {
            **_trip("trip", "Jets at Titans", "2026-05-01", "2026-05-02"),
            "location": "Nashville, TN",
        },
    ]
    schedule_path.write_text(json.dumps(schedule))
    code, _, _ = _run(module, monkeypatch, capsys)
    assert code == 0
    db = json.loads(db_path.read_text())
    assert db["trips"][next(iter(db["trips"]))]["destination"] == "Nashville, TN"


def test_destination_omitted_when_the_feed_leaves_it_blank(build_travel_db, monkeypatch, capsys):
    """An unlabelled trip carries no destination at all. Writing "" would be a
    claim about where the trip goes; absence says the feed never said."""
    module, schedule_path, db_path = build_travel_db
    schedule = [
        {**_trip("trip-blank", "Unlabelled", "2026-05-01", "2026-05-02"), "location": "   "},
        _trip("trip-absent", "No Location Key", "2026-05-03", "2026-05-04"),
    ]
    schedule_path.write_text(json.dumps(schedule))
    code, _, _ = _run(module, monkeypatch, capsys)
    assert code == 0
    db = json.loads(db_path.read_text())
    for trip in db["trips"].values():
        assert "destination" not in trip


def test_destination_is_not_decoded_a_second_time(build_travel_db, monkeypatch, capsys):
    """Decoding is the schedule writer's job (#275). A second pass here would
    eat the backslash out of an address that legitimately carries one."""
    module, schedule_path, db_path = build_travel_db
    schedule = [
        {**_trip("trip", "Odd", "2026-05-01", "2026-05-02"), "location": "A\\, B"},
    ]
    schedule_path.write_text(json.dumps(schedule))
    code, _, _ = _run(module, monkeypatch, capsys)
    assert code == 0
    db = json.loads(db_path.read_text())
    assert db["trips"][next(iter(db["trips"]))]["destination"] == "A\\, B"


def test_run_summary_reports_destination(build_travel_db, monkeypatch, capsys):
    """The nightly log shows which trips the booking check will read as local
    without the operator opening the DB."""
    module, schedule_path, _ = build_travel_db
    schedule = [
        {
            **_trip("trip", "Jets at Titans", "2026-05-01", "2026-05-02"),
            "location": "Nashville, TN",
        },
    ]
    schedule_path.write_text(json.dumps(schedule))
    code, out, _ = _run(module, monkeypatch, capsys)
    assert code == 0
    assert json.loads(out)["trips"][0]["destination"] == "Nashville, TN"
