"""Work-list extraction for the seat pass.

Fixtures use the real schedule shape, captured from the deployed nightly sync:
`type: "Flight"`, a `summary` like "DL2957 ATL to YYZ", a UTC `start`, and a
TripIt `uid`. The reference instant is injected, never read from the clock.
"""

import importlib.util
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Fixed reference: every expectation below is relative to this, so the suite
# does not rot as the real date advances.
NOW = "2026-08-09T00:00:00Z"


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


DL2957 = event("DL2957 ATL to YYZ", "2026-08-11T12:05:00Z")
DL2637 = event("DL2637 BNA to ATL", "2026-08-11T10:14:00Z")


def test_parses_the_real_summary_shape():
    flight = uf.flight_from_event(DL2957)
    assert flight["airline"] == "DL"
    assert flight["flight"] == "2957"
    assert flight["origin"] == "ATL"
    assert flight["destination"] == "YYZ"
    assert flight["date"] == "2026-08-11"


def test_accepts_a_space_between_carrier_and_number():
    assert (
        uf.flight_from_event(event("KL 642 JFK to AMS", "2026-08-31T21:40:00Z"))["flight"] == "642"
    )


def test_ignores_non_flight_events():
    lodging = event("Hilton Amsterdam", "2026-08-31T21:40:00Z", type_="Lodging")
    assert uf.flight_from_event(lodging) is None


def test_ignores_a_flight_whose_summary_does_not_parse():
    """Better to skip than to invent a flight number from prose."""
    odd = event("Rebooked - see email", "2026-08-31T21:40:00Z")
    assert uf.flight_from_event(odd) is None


def test_orders_soonest_first():
    flights = uf.upcoming_flights([DL2957, DL2637], uf._parse_instant(NOW), 12)
    assert [f["flight"] for f in flights] == ["2637", "2957"]


def test_drops_departures_inside_the_lead_window():
    """Too close to act on — check-in has already assigned a seat."""
    soon = event("DL1 ATL to JFK", "2026-08-09T06:00:00Z")
    flights = uf.upcoming_flights([soon, DL2957], uf._parse_instant(NOW), 12)
    assert [f["flight"] for f in flights] == ["2957"]


def test_drops_flights_already_departed():
    past = event("DL9 ATL to JFK", "2026-08-01T06:00:00Z")
    assert uf.upcoming_flights([past], uf._parse_instant(NOW), 12) == []


def test_the_lead_window_is_configurable():
    soon = event("DL1 ATL to JFK", "2026-08-09T06:00:00Z")
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
    assert uf.main(["--schedule", str(path), "--now", NOW]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["count"] == 2
    assert out["flights"][0]["summary"] == "DL2637 BNA to ATL"
