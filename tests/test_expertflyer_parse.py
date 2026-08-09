"""Pin the ExpertFlyer semantics that are easy to get backwards.

The seat-state cases exist because a first pass read grey "wing" shading as
unavailable and reported a full Comfort+ cabin while 13B and 14B were open.
"""

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relpath)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ef = _load("expertflyer_parse", "skills/expertflyer/scripts/expertflyer_parse.py")

NARROWBODY = ["ABC", "DEF"]


# --- seat state: decoration never implies unavailable ------------------------


@pytest.mark.parametrize(
    "marks",
    [
        (),
        ("wing",),
        ("exit_row",),
        ("paid", "premium"),
        ("accessible", "highlighted", "wing"),
    ],
)
def test_decoration_only_seats_are_available(marks):
    assert ef.classify_seat(marks) == "available"


def test_occupied_and_blocked_are_states():
    assert ef.classify_seat(("occupied",)) == "occupied"
    assert ef.classify_seat(("blocked",)) == "blocked"


def test_state_wins_over_decoration():
    assert ef.classify_seat(("wing", "occupied")) == "occupied"
    assert ef.classify_seat(("premium", "blocked")) == "blocked"


def test_blocked_outranks_occupied():
    assert ef.classify_seat(("occupied", "blocked")) == "blocked"


def test_unknown_mark_raises_rather_than_guessing():
    with pytest.raises(ef.UnknownSeatMark):
        ef.classify_seat(("occupied", "quarantined"))


# --- cabin resolution --------------------------------------------------------


@pytest.mark.parametrize(
    "spoken,code",
    [
        ("premium economy", "A"),
        ("Premium Select", "A"),
        ("premium", "A"),
        ("comfort+", "W"),
        ("Comfort Plus", "W"),
        ("economy comfort", "W"),
        ("business", "C"),
        ("Delta One", "C"),
        ("first", "F"),
        ("main cabin", "Y"),
        ("economy", "Y"),
    ],
)
def test_spoken_cabin_names_resolve(spoken, code):
    assert ef.cabin_code(spoken) == code


def test_premium_economy_is_not_comfort_plus():
    assert ef.cabin_code("premium economy") != ef.cabin_code("comfort+")


@pytest.mark.parametrize("code", ["F", "C", "A", "Y", "W", "w"])
def test_bare_codes_pass_through(code):
    assert ef.cabin_code(code) == code.upper()


def test_unknown_cabin_raises_rather_than_defaulting():
    with pytest.raises(ValueError):
        ef.cabin_code("steerage")


# --- seat position -----------------------------------------------------------


@pytest.mark.parametrize(
    "column,expected",
    [
        ("A", "window"),
        ("B", "middle"),
        ("C", "aisle"),
        ("D", "aisle"),
        ("E", "middle"),
        ("F", "window"),
    ],
)
def test_narrowbody_positions(column, expected):
    assert ef.seat_position(column, NARROWBODY) == expected


def test_two_by_two_has_no_middles():
    positions = {c: ef.seat_position(c, ["AB", "CD"]) for c in "ABCD"}
    assert positions == {"A": "window", "B": "aisle", "C": "aisle", "D": "window"}


def test_widebody_three_groups():
    layout = ["ABC", "DEF", "GHK"]
    assert ef.seat_position("A", layout) == "window"
    assert ef.seat_position("K", layout) == "window"
    assert ef.seat_position("D", layout) == "aisle"
    assert ef.seat_position("F", layout) == "aisle"
    assert ef.seat_position("E", layout) == "middle"


def test_column_outside_layout_raises():
    with pytest.raises(ValueError):
        ef.seat_position("Z", NARROWBODY)


# --- matching + alert decision ----------------------------------------------


def _dl2957_comfort_plus():
    """Rows 13-14 as rendered: everything occupied except the two wing middles."""
    seats = []
    for row in (13, 14):
        for col in "ABCDEF":
            marks = ("wing",) if col == "B" else ("occupied", "wing")
            seats.append({"row": row, "column": col, "marks": marks})
    return seats


def test_wing_middles_are_found_not_hidden():
    seats = _dl2957_comfort_plus()
    assert ef.matching_seats(seats, ("middle",), NARROWBODY) == ["13B", "14B"]


def test_no_aisle_or_window_free_so_alert_is_warranted():
    seats = _dl2957_comfort_plus()
    matches = ef.matching_seats(seats, ("aisle", "window"), NARROWBODY)
    assert matches == []
    assert ef.recommend_alert(matches) is True


def test_a_free_aisle_suppresses_the_alert():
    seats = _dl2957_comfort_plus()
    seats.append({"row": 15, "column": "C", "marks": ()})
    matches = ef.matching_seats(seats, ("aisle", "window"), NARROWBODY)
    assert matches == ["15C"]
    assert ef.recommend_alert(matches) is False


def test_unknown_wanted_position_raises():
    with pytest.raises(ValueError):
        ef.matching_seats([], ("porthole",), NARROWBODY)


# --- inventory buckets -------------------------------------------------------


def test_zero_bucket_is_an_answer_not_a_gap():
    assert ef.parse_bucket("Z0") == {
        "class": "Z",
        "seats": 0,
        "available": False,
        "display_capped": False,
    }


def test_nine_is_capped_meaning_at_least_nine():
    parsed = ef.parse_bucket("J9")
    assert parsed["seats"] == 9
    assert parsed["display_capped"] is True
    assert parsed["available"] is True


def test_mid_range_bucket_is_not_capped():
    assert ef.parse_bucket("Z4")["display_capped"] is False


def test_non_bucket_token_raises():
    with pytest.raises(ValueError):
        ef.parse_bucket("Boeing 781")


# --- URL builders ------------------------------------------------------------


def test_availability_url_pins_the_query_contract():
    url = ef.availability_url("jfk", "ams", "2026-08-31", "kl", "z")
    assert url.startswith("https://www.expertflyer.com/air/availability/results?")
    for fragment in (
        "origin=JFK",
        "destination=AMS",
        "departureDateTime=2026-08-31T00%3A00",
        "airLineCodes=KL",
        "classFilter=Z",
        "excludeCodeshares=true",
    ):
        assert fragment in url


def test_codeshares_can_be_included():
    url = ef.availability_url("JFK", "AMS", "2026-08-31", "KL", "Z", exclude_codeshares=False)
    assert "excludeCodeshares=false" in url


def test_status_url_takes_a_bare_date():
    url = ef.status_url("DL", "2957", "2026-08-11")
    assert "departureDateTime=2026-08-11" in url
    assert "airlineCode=DL" in url
    assert "flightNumber=2957" in url


def test_seat_map_url_carries_cabin_and_pax_defaults():
    url = ef.seat_map_url("ATL", "YYZ", "2026-08-11", "DL", "2957", "w")
    for fragment in (
        "departingAirport=ATL",
        "arrivingAirport=YYZ",
        "departDate=2026-08-11",
        "cabinClass=W",
        "paxID=passenger1",
        "ptc=ADT",
    ):
        assert fragment in url
