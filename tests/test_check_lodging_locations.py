"""Tests for check-travel-bookings/scripts/check-lodging-locations.py.

Locks the garbage-lodging-location detector:

  - `garbage_reason` flags only enumerable non-address shapes (blank,
    currency amount, rate/fee keyword) — real addresses and out-of-scope
    shapes (a phone number) pass
  - `find_garbage_lodging` warns once per UPCOMING stay (the Check-in
    record), skips past stays and Check-out records, sorts by date
  - `load_schedule` is tolerant: missing / non-list resolve to None
  - `main` emits the JSON contract and exits 0 even with no schedule

`find_garbage_lodging` takes `today` as an argument, so scans are
deterministic without freezing the clock (per `coding-policy:
testing-standards`); fixtures use fixed past dates.
"""

from __future__ import annotations

import json
from datetime import date


def _lodging(summary: str, start: str, location, *, checkout: bool = False) -> dict:
    if checkout:
        summary = summary.replace("Check-in:", "Check-out:")
    return {
        "type": "Lodging",
        "summary": summary,
        "start": start,
        "end": start,
        "location": location,
    }


_TODAY = date(2026, 8, 2)


# --- garbage_reason ---------------------------------------------------------


def test_reason_currency_amount(check_lodging_locations):
    module, _ = check_lodging_locations
    assert module.garbage_reason("Stay resort fee: $72.03") is not None
    assert module.garbage_reason("€25 per night") is not None
    assert module.garbage_reason("₪500") is not None


def test_reason_fee_keyword_without_currency(check_lodging_locations):
    module, _ = check_lodging_locations
    assert module.garbage_reason("Nightly rate applies") is not None


def test_reason_blank_or_non_string(check_lodging_locations):
    module, _ = check_lodging_locations
    assert module.garbage_reason("") == "no address in TripIt"
    assert module.garbage_reason("   ") == "no address in TripIt"
    assert module.garbage_reason(None) == "no address in TripIt"


def test_reason_real_addresses_pass(check_lodging_locations):
    module, _ = check_lodging_locations
    for addr in (
        "136, Azrieli Center, Menakhem Begin Rd 5, Tel Aviv-Yafo, Israel",
        "Sommerrogata 1 Oslo 00 0255 NO",
        "611 Historic Nature Trail Gatlinburg TN 37738 US",
        "115 Devine St, San Antonio, TX 78210, USA",
        "42 Main St, Deposit, NY 13754, USA",  # a real place named "Deposit"
    ):
        assert module.garbage_reason(addr) is None, addr


def test_reason_out_of_scope_shape_passes(check_lodging_locations):
    """A phone number is garbage but not an enumerated shape — the detector
    stays conservative and does not flag it (better than risking a real
    address). Documents the deliberate scope boundary."""
    module, _ = check_lodging_locations
    assert module.garbage_reason("+1-555-0100") is None


# --- find_garbage_lodging ---------------------------------------------------


def test_flags_upcoming_garbage_stay(check_lodging_locations):
    module, _ = check_lodging_locations
    schedule = [
        _lodging(
            "Check-in: Hilton Amsterdam Airport Schiphol",
            "2026-09-07T20:00:00Z",
            "Stay resort fee: $72.03",
        ),
    ]
    out = module.find_garbage_lodging(schedule, _TODAY)
    assert len(out) == 1
    assert out[0]["hotel"] == "Hilton Amsterdam Airport Schiphol"
    assert out[0]["location"] == "Stay resort fee: $72.03"
    assert out[0]["checkin"] == "2026-09-07"


def test_ignores_checkout_record_so_one_warning_per_stay(check_lodging_locations):
    module, _ = check_lodging_locations
    schedule = [
        _lodging("Check-in: Bad Hotel", "2026-09-07T20:00:00Z", "$99"),
        _lodging("Check-in: Bad Hotel", "2026-09-10T11:00:00Z", "$99", checkout=True),
    ]
    assert len(module.find_garbage_lodging(schedule, _TODAY)) == 1


def test_ignores_past_stays(check_lodging_locations):
    module, _ = check_lodging_locations
    schedule = [_lodging("Check-in: Old Hotel", "2026-07-01T20:00:00Z", "$50")]
    assert module.find_garbage_lodging(schedule, _TODAY) == []


def test_valid_address_not_flagged(check_lodging_locations):
    module, _ = check_lodging_locations
    schedule = [
        _lodging("Check-in: Good Hotel", "2026-09-07T20:00:00Z", "12 Main St, Springfield, US")
    ]
    assert module.find_garbage_lodging(schedule, _TODAY) == []


def test_sorted_by_checkin_date(check_lodging_locations):
    module, _ = check_lodging_locations
    schedule = [
        _lodging("Check-in: Later", "2026-10-01T20:00:00Z", ""),
        _lodging("Check-in: Sooner", "2026-09-01T20:00:00Z", "$10"),
    ]
    out = module.find_garbage_lodging(schedule, _TODAY)
    assert [w["hotel"] for w in out] == ["Sooner", "Later"]


def test_non_lodging_and_none_schedule_ignored(check_lodging_locations):
    module, _ = check_lodging_locations
    assert module.find_garbage_lodging(None, _TODAY) == []
    assert module.find_garbage_lodging([{"type": "Flight", "summary": "x"}], _TODAY) == []


# --- load_schedule + main ---------------------------------------------------


def test_load_schedule_missing_returns_none(check_lodging_locations):
    module, schedule_path = check_lodging_locations
    assert module.load_schedule(str(schedule_path)) is None


def test_load_schedule_non_list_returns_none(check_lodging_locations):
    module, schedule_path = check_lodging_locations
    schedule_path.write_text(json.dumps({"not": "a list"}))
    assert module.load_schedule(str(schedule_path)) is None


def test_main_emits_contract_and_exits_zero_without_schedule(check_lodging_locations, capsys):
    module, _ = check_lodging_locations
    module.main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["garbage_lodging"] == []
    assert "checked_at" in payload


class _FrozenDate(date):
    @classmethod
    def today(cls):
        return date(2020, 1, 1)


def test_main_reports_garbage_when_present(check_lodging_locations, capsys, monkeypatch):
    """main() reads the clock, so freeze `today` to a fixed instant and use a
    fixed past check-in AFTER it — no future-date literal (testing-standards)."""
    module, schedule_path = check_lodging_locations
    monkeypatch.setattr(module, "date", _FrozenDate)
    schedule_path.write_text(
        json.dumps([_lodging("Check-in: Bad Hotel", "2020-06-01T20:00:00Z", "Resort fee: $30")])
    )
    module.main()
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["garbage_lodging"]) == 1
    assert payload["garbage_lodging"][0]["hotel"] == "Bad Hotel"
