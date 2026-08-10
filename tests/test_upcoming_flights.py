"""Work-list extraction for the seat pass.

Fixtures use the real schedule shape, captured from the deployed nightly sync:
`type: "Flight"`, a `summary` like "DL2957 ATL to YYZ", a UTC `start`, and a
TripIt `uid`.

The timeline is fixed and entirely in the PAST, with the real intervals
preserved: the reference instant, a departure two days out, one six hours out,
and one already gone. Nothing here touches a live upstream, so no future date
is needed and one would only rot.
"""

import importlib.util
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Fixed reference: every expectation below is relative to this, so the suite
# does not rot as the real date advances.
NOW = "2024-03-05T00:00:00Z"


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relpath)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


uf = _load("upcoming_flights", "skills/expertflyer/scripts/upcoming-flights.py")


def event(summary, start, type_="Flight", uid=None):
    return {
        "schema_version": "2",
        "summary": summary,
        "start": start,
        "end": start,
        "location": "Atlanta (ATL)",
        "type": type_,
        "uid": uid or f"item-{summary}@tripit.com",
    }


DL2957 = event("DL2957 ATL to YYZ", "2024-03-07T12:05:00Z")
DL2637 = event("DL2637 BNA to ATL", "2024-03-07T10:14:00Z")


def test_parses_the_real_summary_shape():
    flight = uf.flight_from_event(DL2957)
    assert flight["airline"] == "DL"
    assert flight["flight"] == "2957"
    assert flight["origin"] == "ATL"
    assert flight["destination"] == "YYZ"
    assert flight["date"] == "2024-03-07"


def test_accepts_a_space_between_carrier_and_number():
    assert (
        uf.flight_from_event(event("KL 642 JFK to AMS", "2024-03-27T21:40:00Z"))["flight"] == "642"
    )


def test_ignores_non_flight_events():
    lodging = event("Hilton Amsterdam", "2024-03-27T21:40:00Z", type_="Lodging")
    assert uf.flight_from_event(lodging) is None


def test_ignores_a_flight_whose_summary_does_not_parse():
    """Better to skip than to invent a flight number from prose."""
    odd = event("Rebooked - see email", "2024-03-27T21:40:00Z")
    assert uf.flight_from_event(odd) is None


def test_orders_soonest_first():
    flights = uf.upcoming_flights([DL2957, DL2637], uf._parse_instant(NOW), 12)
    assert [f["flight"] for f in flights] == ["2637", "2957"]


def test_drops_departures_inside_the_lead_window():
    """Too close to act on — check-in has already assigned a seat."""
    soon = event("DL1 ATL to JFK", "2024-03-05T06:00:00Z")
    flights = uf.upcoming_flights([soon, DL2957], uf._parse_instant(NOW), 12)
    assert [f["flight"] for f in flights] == ["2957"]


def test_drops_flights_already_departed():
    past = event("DL9 ATL to JFK", "2024-02-26T06:00:00Z")
    assert uf.upcoming_flights([past], uf._parse_instant(NOW), 12) == []


def test_the_lead_window_is_configurable():
    soon = event("DL1 ATL to JFK", "2024-03-05T06:00:00Z")
    assert uf.upcoming_flights([soon], uf._parse_instant(NOW), 0)[0]["flight"] == "1"


def test_a_resynced_duplicate_segment_is_reported_once():
    """The same uid can reappear after a schedule refresh."""
    flights = uf.upcoming_flights([DL2957, dict(DL2957)], uf._parse_instant(NOW), 12)
    assert len(flights) == 1


def test_two_different_flights_are_not_collapsed():
    flights = uf.upcoming_flights([DL2957, DL2637], uf._parse_instant(NOW), 12)
    assert len(flights) == 2


def test_non_dict_entries_are_skipped_rather_than_crashing():
    assert uf.upcoming_flights([None, "junk", DL2957], uf._parse_instant(NOW), 12)


def test_missing_schedule_reports_actionably(tmp_path, capsys):
    code = uf.main(["--schedule", str(tmp_path / "absent.json"), "--now", NOW])
    assert code == 1
    assert json.loads(capsys.readouterr().out)["error"] == "no_schedule"


def test_invalid_schedule_json_reports_actionably(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    code = uf.main(["--schedule", str(bad), "--now", NOW])
    assert code == 1
    assert json.loads(capsys.readouterr().out)["error"] == "bad_schedule"


def test_end_to_end_against_the_real_schedule_shape(tmp_path, capsys):
    path = tmp_path / "travel-schedule.json"
    path.write_text(json.dumps([DL2637, DL2957]))
    # `--trips 0` — this pins the schedule shape, not the trip bound.
    assert uf.main(["--schedule", str(path), "--now", NOW, "--trips", "0"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["count"] == 2
    assert out["flights"][0]["summary"] == "DL2637 BNA to ATL"


# --- input faults are reported, never raised ---------------------------------


def test_a_bad_reference_instant_reports_rather_than_tracebacks(tmp_path, capsys):
    path = tmp_path / "s.json"
    path.write_text(json.dumps([DL2957]))
    code = uf.main(["--schedule", str(path), "--now", "not-a-date"])
    assert code == 1
    assert json.loads(capsys.readouterr().out)["error"] == "bad_now"


def test_a_json_scalar_root_reports_rather_than_tracebacks(tmp_path, capsys):
    """Valid JSON, wrong shape — parses fine then explodes on iteration."""
    path = tmp_path / "s.json"
    path.write_text("42")
    code = uf.main(["--schedule", str(path), "--now", NOW])
    assert code == 1
    assert json.loads(capsys.readouterr().out)["error"] == "bad_schedule"


def test_an_unparseable_timestamp_skips_one_event_not_the_schedule():
    broken = event("DL5 ATL to JFK", "not-a-timestamp")
    flights = uf.upcoming_flights([broken, DL2957], uf._parse_instant(NOW), 12)
    assert [f["flight"] for f in flights] == ["2957"]


def test_a_missing_start_skips_the_event():
    no_start = {k: v for k, v in DL2957.items() if k != "start"}
    assert uf.flight_from_event(no_start) is None


def test_a_dict_root_with_an_events_key_is_accepted(tmp_path, capsys):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"events": [DL2957]}))
    assert uf.main(["--schedule", str(path), "--now", NOW, "--trips", "0"]) == 0
    assert json.loads(capsys.readouterr().out)["count"] == 1


def test_same_flight_number_on_two_legs_is_not_collapsed():
    """A through flight keeps its number across legs on the same date."""
    leg1 = {**event("DL100 ATL to LAX", "2024-03-07T12:00:00Z"), "uid": None}
    leg2 = {**event("DL100 LAX to HNL", "2024-03-07T18:00:00Z"), "uid": None}
    flights = uf.upcoming_flights([leg1, leg2], uf._parse_instant(NOW), 12)
    assert [f["destination"] for f in flights] == ["LAX", "HNL"]


def test_an_unreadable_schedule_reports_rather_than_tracebacks(tmp_path, capsys):
    """Not valid UTF-8 — a real failure mode for a synced file."""
    path = tmp_path / "s.json"
    path.write_bytes(b"\xff\xfe not utf-8")
    code = uf.main(["--schedule", str(path), "--now", NOW])
    assert code == 1
    assert json.loads(capsys.readouterr().out)["error"] == "unreadable_schedule"


def test_a_permission_failure_reports_rather_than_tracebacks(tmp_path, capsys, monkeypatch):
    """Mocked rather than chmod: a privileged test user can read 0o000 anyway."""
    path = tmp_path / "s.json"
    path.write_text(json.dumps([DL2957]))

    def deny(self, *args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(uf.Path, "read_text", deny)
    code = uf.main(["--schedule", str(path), "--now", NOW])
    assert code == 1
    assert json.loads(capsys.readouterr().out)["error"] == "unreadable_schedule"


def test_every_failure_diagnostic_names_a_recovery_action(tmp_path, capsys):
    """Actionable Messages: say what to do, not only what broke."""
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    uf.main(["--schedule", str(bad), "--now", NOW])
    assert "nightly-travel-sync" in capsys.readouterr().err

    scalar = tmp_path / "scalar.json"
    scalar.write_text("42")
    uf.main(["--schedule", str(scalar), "--now", NOW])
    assert "nightly-travel-sync" in capsys.readouterr().err

    uf.main(["--schedule", str(tmp_path / "absent.json"), "--now", NOW])
    assert "nightly-travel-sync" in capsys.readouterr().err


# --- the pass is bounded to the next trip -----------------------------------


def trip(summary, start, end, uid=None):
    return {
        "schema_version": "2",
        "summary": summary,
        "start": start,
        "end": end,
        "type": "Trip",
        "uid": uid,
    }


def test_the_pass_covers_the_next_trip_and_reports_what_it_dropped():
    """Every flight costs a request per cabin against a bot-walled service, so
    the whole upcoming schedule is not a work list."""
    events = [
        trip("Toronto", "2024-03-06", "2024-03-07", uid="t1"),
        trip("San Francisco", "2024-03-20", "2024-03-24", uid="t2"),
        event("DL2637 BNA to ATL", "2024-03-06T10:14:00Z", uid="f1"),
        event("DL2957 ATL to YYZ", "2024-03-06T12:05:00Z", uid="f2"),
        event("DL891 BNA to LAX", "2024-03-20T11:20:00Z", uid="f3"),
    ]
    now = uf._parse_instant(NOW)
    flights = uf.upcoming_flights(events, now, uf.MIN_LEAD_HOURS)
    trips = uf.upcoming_trips(events, now, 1)
    covered = [f for f in flights if uf._in_any_trip(trips, f)]
    assert [f["flight"] for f in covered] == ["2637", "2957"]
    assert [t["summary"] for t in trips] == ["Toronto"]


def test_a_late_local_departure_stays_inside_its_own_trip():
    """The schedule stamps UTC, so a 22:50 local return lands on the next UTC
    day and falls a day past a trip that ends the evening before."""
    events = [
        trip("Toronto", "2024-03-06", "2024-03-07", uid="t1"),
        event("DL3121 ATL to BNA", "2024-03-08T02:50:00Z", uid="f1"),
    ]
    now = uf._parse_instant(NOW)
    trips = uf.upcoming_trips(events, now, 1)
    flights = uf.upcoming_flights(events, now, uf.MIN_LEAD_HOURS)
    assert [f for f in flights if uf._in_any_trip(trips, f)] == flights


def test_widening_to_more_trips_reaches_the_later_one():
    events = [
        trip("Toronto", "2024-03-06", "2024-03-07", uid="t1"),
        trip("San Francisco", "2024-03-20", "2024-03-24", uid="t2"),
    ]
    now = uf._parse_instant(NOW)
    assert [t["summary"] for t in uf.upcoming_trips(events, now, 2)] == [
        "Toronto",
        "San Francisco",
    ]
    # Zero means every upcoming trip, not none.
    assert len(uf.upcoming_trips(events, now, 0)) == 2


def test_a_trip_already_over_is_not_upcoming():
    events = [trip("Past", "2024-02-01", "2024-02-03", uid="t0")]
    assert uf.upcoming_trips(events, uf._parse_instant(NOW), 1) == []


def test_a_trip_missing_its_window_is_skipped_not_guessed():
    assert uf.trip_from_event({"type": "Trip", "summary": "No dates"}) is None
    assert uf.trip_from_event({"type": "Trip", "start": "2024-03-06"}) is None
    assert uf.trip_from_event({"type": "Trip", "start": "nonsense", "end": "x"}) is None


def test_the_output_names_the_trips_and_the_flights_it_left_out(tmp_path, capsys):
    events = [
        trip("Toronto", "2024-03-06", "2024-03-07", uid="t1"),
        event("DL2637 BNA to ATL", "2024-03-06T10:14:00Z", uid="f1"),
        event("DL891 BNA to LAX", "2024-03-20T11:20:00Z", uid="f3"),
    ]
    path = tmp_path / "schedule.json"
    path.write_text(json.dumps(events))
    assert uf.main(["--schedule", str(path), "--now", NOW]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [f["flight"] for f in out["flights"]] == ["2637"]
    assert out["excluded_count"] == 1
    assert [f["flight"] for f in out["excluded"]] == ["891"]
    assert [t["summary"] for t in out["trips"]] == ["Toronto"]
    # The internal window bounds are not part of the contract.
    assert "opens" not in out["trips"][0]


def test_trips_zero_covers_every_upcoming_flight(tmp_path, capsys):
    events = [
        trip("Toronto", "2024-03-06", "2024-03-07", uid="t1"),
        event("DL2637 BNA to ATL", "2024-03-06T10:14:00Z", uid="f1"),
        event("DL891 BNA to LAX", "2024-03-20T11:20:00Z", uid="f3"),
    ]
    path = tmp_path / "schedule.json"
    path.write_text(json.dumps(events))
    assert uf.main(["--schedule", str(path), "--now", NOW, "--trips", "0"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["count"] == 2
    assert out["excluded_count"] == 0


def test_a_schedule_with_no_trips_covers_nothing_and_says_so(tmp_path, capsys):
    """A flight belonging to no trip is dropped, and dropping it silently would
    read as 'your seats are fine' on a trip never looked at."""
    events = [event("DL2637 BNA to ATL", "2024-03-06T10:14:00Z", uid="f1")]
    path = tmp_path / "schedule.json"
    path.write_text(json.dumps(events))
    assert uf.main(["--schedule", str(path), "--now", NOW]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["count"] == 0
    assert out["excluded_count"] == 1
    assert out["trips"] == []
