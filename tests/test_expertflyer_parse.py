"""Pin the ExpertFlyer semantics that are easy to get backwards.

The seat fixture reproduces DL2957 ATL-YYZ Comfort+ as the live RSC payload
returned it: every seat occupied except 13B and 14B, which are middles. That
shape is the regression guard — an earlier pass read those two as unavailable
and reported a full cabin while they were open on delta.com.
"""

import importlib.util
import json
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


def _seat(label, status, position):
    return {
        "label": label,
        "status": status,
        "type": "seat",
        "isWindow": position == "window",
        "isAisle": position == "aisle",
        "isMiddle": position == "middle",
    }


POSITIONS = {"A": "window", "B": "middle", "C": "aisle", "D": "aisle", "E": "middle", "F": "window"}


def dl2957_seat_map():
    """Comfort+ rows 10-14; only 13B and 14B are free."""
    rows = []
    for number in (11, 12, 13, 14):
        seats = []
        for col in "ABC":
            status = "available" if (col == "B" and number in (13, 14)) else "occupied"
            seats.append(_seat(col, status, POSITIONS[col]))
        seats.append({"type": "aisle"})  # gap cell: no status, must be skipped
        for col in "DEF":
            seats.append(_seat(col, "occupied", POSITIONS[col]))
        rows.append({"rowNumber": number, "wing": number >= 13, "seats": seats})
    return {"sections": [{"columns": [{"label": c} for c in "ABC DEF"], "rows": rows}]}


# --- seat availability -------------------------------------------------------


def test_available_status_is_bookable():
    assert ef.seat_available(_seat("B", "available", "middle")) is True


@pytest.mark.parametrize("status", ["occupied", "blocked"])
def test_taken_statuses_are_not_bookable(status):
    assert ef.seat_available(_seat("A", status, "window")) is False


def test_unknown_status_raises_rather_than_guessing():
    with pytest.raises(ef.UnknownSeatStatus):
        ef.seat_available({"label": "A", "status": "quarantined", "type": "seat"})


def test_position_comes_from_the_site_not_a_heuristic():
    assert ef.seat_position(_seat("A", "available", "window")) == "window"
    assert ef.seat_position(_seat("C", "available", "aisle")) == "aisle"
    assert ef.seat_position(_seat("B", "available", "middle")) == "middle"


def test_aisle_gap_cells_are_skipped():
    seats = list(ef.iter_seats(dl2957_seat_map()))
    assert all(s.get("type") == "seat" for _, s in seats)
    assert len(seats) == 4 * 6


# --- the regression that started this ---------------------------------------


def test_free_middles_are_found_not_hidden():
    assert ef.available_seats(dl2957_seat_map()) == ["13B", "14B"]


def test_wing_rows_do_not_suppress_availability():
    """13 and 14 are wing rows; wing is decoration, never a state."""
    free = ef.available_seats(dl2957_seat_map())
    assert "13B" in free and "14B" in free


def test_no_aisle_or_window_free_so_alert_is_warranted():
    matches = ef.matching_seats(dl2957_seat_map(), "non-middle")
    assert matches == []
    assert ef.recommend_alert(matches) is True


def test_a_free_aisle_suppresses_the_alert():
    seat_map = dl2957_seat_map()
    seat_map["sections"][0]["rows"][0]["seats"][2] = _seat("C", "available", "aisle")
    matches = ef.matching_seats(seat_map, "non-middle")
    assert matches == ["11C"]
    assert ef.recommend_alert(matches) is False


def test_middle_request_finds_the_wing_middles():
    assert ef.matching_seats(dl2957_seat_map(), ["middle"]) == ["13B", "14B"]


def test_any_matches_every_free_seat():
    assert ef.matching_seats(dl2957_seat_map(), "any") == ["13B", "14B"]


# --- wanted-position normalisation ------------------------------------------


@pytest.mark.parametrize("spec", ["non-middle", "nonmiddle", "non middle", "not middle"])
def test_non_middle_expands_to_aisle_and_window(spec):
    assert ef.normalize_wants(spec) == ("aisle", "window")


def test_comma_string_and_iterable_agree():
    assert ef.normalize_wants("aisle,window") == ef.normalize_wants(["aisle", "window"])


def test_duplicates_collapse():
    assert ef.normalize_wants("aisle,non-middle") == ("aisle", "window")


def test_criterion_values_are_the_checkbox_value_attributes():
    assert ef.criterion_values("non-middle") == ("AISLE", "WINDOW")
    assert ef.criterion_values("any") == ("ANY",)


def test_middle_is_searchable_but_not_alertable():
    """The seat map reports middles; the alert form has no middle checkbox."""
    assert ef.normalize_wants("middle") == ("middle",)
    with pytest.raises(ValueError, match="no alert criterion"):
        ef.criterion_values("middle")


def test_unknown_want_raises():
    with pytest.raises(ValueError):
        ef.normalize_wants("porthole")


def test_empty_want_raises():
    with pytest.raises(ValueError):
        ef.normalize_wants("")


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


# --- RSC extraction ----------------------------------------------------------


def test_extract_json_object_from_streaming_payload():
    payload = '0:{"a":"$@1"}\n1:[{"seatMap":' + json.dumps(dl2957_seat_map()) + ',"other":1}]'
    extracted = ef.extract_json_object(payload, "seatMap")
    assert ef.available_seats(extracted) == ["13B", "14B"]


def test_extract_missing_key_raises():
    with pytest.raises(KeyError):
        ef.extract_json_object('0:{"nope":1}', "seatMap")


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


def test_seat_map_url_requests_the_structured_payload():
    url = ef.seat_map_url("ATL", "YYZ", "2026-08-11", "DL", "2957", "w")
    for fragment in (
        "departingAirport=ATL",
        "arrivingAirport=YYZ",
        "departDate=2026-08-11",
        "cabinClass=W",
        "withRawXML=true",
    ):
        assert fragment in url


# --- availability payload parsing -------------------------------------------


def _availability_payload(*flights):
    """Mimic the RSC shape: identity fields then bookingClassAvailability."""
    chunks = []
    for f in flights:
        classes = json.dumps(
            [
                {
                    "code": code,
                    "codeDescription": "Business",
                    "cabin": "C",
                    "availability": seats,
                    "hasAvailability": seats > 0,
                }
                for code, seats in f["classes"].items()
            ]
        )
        chunks.append(
            f'"departureAirport":"{f["origin"]}","arrivalAirport":"{f["dest"]}",'
            f'"marketingAirlineCode":"{f["marketing"]}",'
            + (f'"operatingAirlineCode":"{f["operating"]}",' if f.get("operating") else "")
            + f'"flightNumber":"{f["number"]}","departureDateTime":"{f["dep"]}",'
            f'"airEquipType":"{f["equip"]}","bookingClassAvailability":{classes}'
        )
    return "1:[{" + "},{".join(chunks) + "}]"


def test_availability_flights_reads_structured_buckets():
    payload = _availability_payload(
        {
            "origin": "JFK",
            "dest": "AMS",
            "marketing": "KL",
            "number": "642",
            "dep": "08-31T16:40",
            "equip": "781",
            "classes": {"J": 7, "Z": 0},
        },
        {
            "origin": "JFK",
            "dest": "AMS",
            "marketing": "KL",
            "number": "646",
            "dep": "08-31T19:10",
            "equip": "77W",
            "classes": {"J": 9, "Z": 1},
        },
    )
    flights = ef.availability_flights(payload)
    assert [f["flight_number"] for f in flights] == ["642", "646"]
    assert flights[0]["classes"]["Z"] == {
        "seats": 0,
        "available": False,
        "display_capped": False,
        "cabin": "C",
    }
    assert flights[1]["classes"]["Z"]["available"] is True
    assert flights[1]["classes"]["J"]["display_capped"] is True


def test_availability_never_invents_flights_from_time_tokens():
    """Text scraping produced 'PM787' by gluing an aircraft code to an AM/PM."""
    payload = _availability_payload(
        {
            "origin": "JFK",
            "dest": "AMS",
            "marketing": "KL",
            "number": "642",
            "dep": "08-31T16:40",
            "equip": "781",
            "classes": {"Z": 0},
        }
    )
    numbers = {f["flight_number"] for f in ef.availability_flights(payload)}
    assert numbers == {"642"}
    assert "787" not in numbers and "781" not in numbers


def test_codeshare_is_flagged_from_operating_carrier():
    payload = _availability_payload(
        {
            "origin": "JFK",
            "dest": "AMS",
            "marketing": "KL",
            "operating": "N0",
            "number": "1956",
            "dep": "08-31T17:25",
            "equip": "32Q",
            "classes": {"Z": 1},
        }
    )
    flight = ef.availability_flights(payload)[0]
    assert flight["is_codeshare"] is True
    assert flight["operating_carrier"] == "N0"


def test_find_flight_tolerates_leading_zeros():
    payload = _availability_payload(
        {
            "origin": "JFK",
            "dest": "AMS",
            "marketing": "KL",
            "number": "0642",
            "dep": "08-31T16:40",
            "equip": "781",
            "classes": {"Z": 0},
        }
    )
    flights = ef.availability_flights(payload)
    assert ef.find_flight(flights, "642") is not None
    assert ef.find_flight(flights, "646") is None


# --- alert payload parsing ---------------------------------------------------


def test_extract_alerts_dedupes_by_id():
    alert = {
        "alertType": "SEAT_MAP",
        "status": "ACTIVE",
        "id": 5736636,
        "airlineCode": "DL",
        "flightNumber": 2957,
        "departAirportCode": "ATL",
        "arriveAirportCode": "YYZ",
        "classCode": "W",
        "seatMapLocations": ["AISLE", "WINDOW"],
        "name": "ATL to YYZ DL2957 8.11.26",
    }
    payload = "0:{}\n1:" + json.dumps([alert]) + "\n2:" + json.dumps([alert])
    alerts = ef.extract_alerts(payload)
    assert len(alerts) == 1
    assert alerts[0]["seatMapLocations"] == ["AISLE", "WINDOW"]


def test_extract_alerts_survives_braces_inside_names():
    """A hand-rolled brace counter desynchronises on punctuation in a name."""
    alert = {
        "alertType": "SEAT_MAP",
        "status": "ACTIVE",
        "id": 1,
        "name": "weird [name] {with} brackets",
        "flightNumber": 1,
    }
    alerts = ef.extract_alerts("1:" + json.dumps([alert]))
    assert alerts[0]["name"] == "weird [name] {with} brackets"


def test_extract_alerts_empty_when_absent():
    assert ef.extract_alerts('0:{"nothing":true}') == []
