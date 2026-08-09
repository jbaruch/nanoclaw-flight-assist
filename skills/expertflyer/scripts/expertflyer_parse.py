"""Pure parsing and decision helpers for the ExpertFlyer skill.

No browser, no network — everything here is a pure function so the semantics
that are easy to get backwards (seat availability, capped inventory, cabin
identity, alert-worthiness) are pinned by tests rather than by a comment.

Seat data arrives as structured JSON inside the page's RSC payload, which
states each seat's `status` and its own `isWindow`/`isAisle`/`isMiddle` flags.
That is ground truth; do not re-derive position from a column-letter heuristic
and do not infer availability from the rendered legend.

See ../references/web-contract.md.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlencode

BASE = "https://www.expertflyer.com"

# Seat cell statuses. Only "available" is bookable; an unrecognised status
# raises rather than defaulting, because guessing either way is a real error:
# guess available and the operator chases a seat that is not there, guess
# unavailable and an open seat gets hidden behind a pointless alert.
BOOKABLE_STATUS = "available"
KNOWN_STATUSES = frozenset({"available", "occupied", "blocked"})

# Non-seat cells (the aisle gap) carry a type but no status.
SEAT_TYPE = "seat"

# Inventory digits saturate at 9: "Z9" means at least 9, not exactly 9.
DISPLAY_CAP = 9
BUCKET_RE = re.compile(r"^([A-Z])(\d{1,2})$")

# Cabin the operator names -> the code the seat-map form wants. Premium economy
# is Delta's "Premium Select" (A) and is NOT Comfort+ (W); conflating them
# searches the wrong cabin and reports seats the operator cannot book.
CABIN_ALIASES = {
    "first": "F",
    "first class": "F",
    "business": "C",
    "business class": "C",
    "delta one": "C",
    "premium select": "A",
    "premium economy": "A",
    "premium econ": "A",
    "prem econ": "A",
    "premium": "A",
    "comfort plus": "W",
    "comfort": "W",
    "economy comfort": "W",
    "main cabin extra": "W",
    "economy": "Y",
    "main cabin": "Y",
    "coach": "Y",
}
CABIN_CODES = frozenset({"F", "C", "A", "Y", "W"})

# What a seat search may ask for. Wider than what an alert can watch: the seat
# map reports middles, but the alert form offers no middle checkbox.
QUERY_POSITIONS = frozenset({"any", "aisle", "window", "middle", "exit", "two_together"})

# The alert form's criteria checkboxes are bound by their `value` attribute.
# Index ordering shifts and the label text sits in a nested div that reads
# empty from the parent, so value is the only stable key.
SEAT_CRITERION_VALUES = {
    "any": "ANY",
    "aisle": "AISLE",
    "window": "WINDOW",
    "exit": "EXIT",
    "two_together": "TWO_TOGETHER",
}
# "Non-middle" is not a control on the form — it is aisle OR window.
WANT_EXPANSIONS = {
    "non-middle": ("aisle", "window"),
    "nonmiddle": ("aisle", "window"),
    "non middle": ("aisle", "window"),
    "not middle": ("aisle", "window"),
}


class UnknownSeatStatus(ValueError):
    """A seat status the parser has not been taught."""


_DECODER = json.JSONDecoder()


def _decode_at(text: str, index: int):
    """Decode the JSON value starting at `index`, returning (value, end).

    Uses the real decoder rather than counting braces: a brace or bracket
    inside a string literal would desynchronise hand-rolled matching.
    """
    return _DECODER.raw_decode(text, index)


def extract_json_object(text: str, key: str, from_index: int = 0) -> dict:
    """Pull one JSON object out of an RSC payload by its key.

    The payload is not valid JSON as a whole (it is line-prefixed React
    streaming format), so locate the key and decode just its value.
    """
    marker = f'"{key}"'
    start = text.find(marker, from_index)
    if start < 0:
        raise KeyError(f"{key!r} not present in payload")
    value, _ = _decode_at(text, text.index("{", start))
    return value


def extract_alerts(payload: str) -> list[dict]:
    """Every alert object the account's payload carries.

    The /alerts page defaults to the Flight Alerts tab and renders
    "No alerts found" even when seat alerts exist, so the rendered text is not
    a usable source of truth — the payload is.
    """
    out: list[dict] = []
    cursor = 0
    while True:
        start = payload.find('[{"alertType"', cursor)
        if start < 0:
            break
        try:
            value, end = _decode_at(payload, start)
        except json.JSONDecodeError:
            cursor = start + 1
            continue
        if isinstance(value, list):
            out.extend(a for a in value if isinstance(a, dict) and "alertType" in a)
        cursor = end
    return list({a.get("id"): a for a in out}.values())


def extract_json_array(text: str, key: str, from_index: int = 0):
    """Brace-match one JSON array value out of an RSC payload."""
    marker = f'"{key}"'
    start = text.find(marker, from_index)
    if start < 0:
        raise KeyError(f"{key!r} not present in payload")
    return _decode_at(text, text.index("[", start))


# Identity fields sit immediately before each flight's bookingClassAvailability.
_IDENTITY_FIELDS = (
    ("marketing_carrier", "marketingAirlineCode"),
    ("operating_carrier", "operatingAirlineCode"),
    ("flight_number", "flightNumber"),
    ("origin", "departureAirport"),
    ("destination", "arrivalAirport"),
    ("departure", "departureDateTime"),
    ("arrival", "arrivalDateTime"),
    ("equipment", "airEquipType"),
)
_IDENTITY_LOOKBACK = 2000


def availability_flights(payload: str) -> list[dict]:
    """Every flight in an availability payload with its fare-class buckets.

    Reads the page's structured `bookingClassAvailability` rather than the
    rendered grid: scraping the text pairs aircraft codes with AM/PM tokens
    from departure times and invents flights like "PM787".
    """
    flights: list[dict] = []
    cursor = 0
    while True:
        try:
            classes, end = extract_json_array(payload, "bookingClassAvailability", cursor)
        except (KeyError, ValueError):
            break
        marker_at = payload.rfind('"bookingClassAvailability"', 0, end)
        head = payload[max(0, marker_at - _IDENTITY_LOOKBACK) : end]
        identity = {}
        for name, field in _IDENTITY_FIELDS:
            found = re.findall(rf'"{field}":"([^"]*)"', head)
            identity[name] = found[-1] if found else None
        marketing = identity["marketing_carrier"]
        identity["operating_carrier"] = identity["operating_carrier"] or marketing
        identity["is_codeshare"] = identity["operating_carrier"] != marketing
        identity["classes"] = {
            c["code"]: {
                "seats": c.get("availability", 0),
                "available": bool(c.get("hasAvailability")),
                "display_capped": (c.get("availability") or 0) >= DISPLAY_CAP,
                "cabin": c.get("cabin") or None,
            }
            for c in classes
            if c.get("code")
        }
        flights.append(identity)
        cursor = end + 1
    return flights


def find_flight(flights, flight_number) -> dict | None:
    """Locate one flight by number among parsed availability flights."""
    wanted = str(flight_number).lstrip("0")
    for f in flights:
        if (f.get("flight_number") or "").lstrip("0") == wanted:
            return f
    return None


def seat_position(seat: dict) -> str:
    """Position as the site itself reports it."""
    if seat.get("isWindow"):
        return "window"
    if seat.get("isAisle"):
        return "aisle"
    if seat.get("isMiddle"):
        return "middle"
    return "unknown"


def seat_available(seat: dict) -> bool:
    """True when the seat is bookable. Raises on an unrecognised status."""
    status = seat.get("status")
    if status not in KNOWN_STATUSES:
        raise UnknownSeatStatus(
            f"unrecognised seat status {status!r} — add it to KNOWN_STATUSES "
            "before trusting this seat map"
        )
    return status == BOOKABLE_STATUS


def iter_seats(seat_map: dict):
    """Yield (row_number, seat) for real seats, skipping aisle-gap cells."""
    for section in seat_map.get("sections", ()):
        for row in section.get("rows", ()):
            for seat in row.get("seats", ()):
                if seat.get("type") != SEAT_TYPE:
                    continue
                yield row.get("rowNumber"), seat


def normalize_wants(spec) -> tuple[str, ...]:
    """Normalise a wanted-seat spec to ordered, de-duplicated position names.

    Accepts a comma string ("aisle,window"), an iterable, or the shorthand
    "non-middle". Raises on anything unrecognised so a typo cannot silently
    widen the search to every seat.
    """
    if isinstance(spec, str):
        parts = [p.strip() for p in spec.split(",") if p.strip()]
    else:
        parts = [str(p).strip() for p in spec if str(p).strip()]
    if not parts:
        raise ValueError("no wanted seat positions given")

    out: list[str] = []
    for part in parts:
        key = re.sub(r"\s+", " ", part.lower())
        for name in WANT_EXPANSIONS.get(key, (key,)):
            if name not in QUERY_POSITIONS:
                raise ValueError(
                    f"unknown seat position {part!r} — expected "
                    f"{sorted(QUERY_POSITIONS)} or 'non-middle'"
                )
            if name not in out:
                out.append(name)
    return tuple(out)


def criterion_values(wants) -> tuple[str, ...]:
    """Map wanted positions to the alert form's checkbox `value` attributes.

    Raises for a position the form cannot watch — ExpertFlyer has no
    middle-seat alert, so "alert me for a middle" must fail loudly rather than
    quietly registering a different criterion.
    """
    wanted = normalize_wants(wants)
    unwatchable = [w for w in wanted if w not in SEAT_CRITERION_VALUES]
    if unwatchable:
        raise ValueError(
            f"ExpertFlyer has no alert criterion for {unwatchable} — "
            f"watchable criteria are {sorted(SEAT_CRITERION_VALUES)}"
        )
    return tuple(SEAT_CRITERION_VALUES[w] for w in wanted)


def matching_seats(seat_map: dict, wants) -> list[str]:
    """Bookable seats whose position matches any wanted position."""
    wanted = set(normalize_wants(wants))
    match_any = "any" in wanted
    out = []
    for row_number, seat in iter_seats(seat_map):
        if not seat_available(seat):
            continue
        if match_any or seat_position(seat) in wanted:
            out.append(f"{row_number}{seat['label']}")
    return out


def available_seats(seat_map: dict) -> list[str]:
    """Every bookable seat, regardless of position."""
    return [f"{r}{s['label']}" for r, s in iter_seats(seat_map) if seat_available(s)]


def recommend_alert(matches) -> bool:
    """An alert is worth creating only when nothing wanted is already free."""
    return not matches


def cabin_code(name: str) -> str:
    """Resolve a cabin the operator named to its ExpertFlyer code.

    Accepts a bare code ('W') or a spoken name ('premium economy'). Raises on
    anything unrecognised rather than defaulting to economy, because a silent
    wrong-cabin search returns seats that look plausible and are not bookable.
    """
    raw = (name or "").strip()
    if raw.upper() in CABIN_CODES:
        return raw.upper()
    normalised = re.sub(r"\s+", " ", raw.lower().replace("+", " plus")).strip()
    if normalised in CABIN_ALIASES:
        return CABIN_ALIASES[normalised]
    raise ValueError(
        f"unknown cabin {name!r} — expected one of {sorted(CABIN_CODES)} or a "
        f"name like {sorted(CABIN_ALIASES)[:4]}"
    )


def parse_bucket(token: str) -> dict:
    """Parse an inventory token like 'Z0' or 'J9'."""
    m = BUCKET_RE.match(token.strip().upper())
    if not m:
        raise ValueError(f"not an inventory bucket: {token!r}")
    seats = int(m.group(2))
    return {
        "class": m.group(1),
        "seats": seats,
        "available": seats > 0,
        "display_capped": seats >= DISPLAY_CAP,
    }


def availability_url(
    origin: str,
    destination: str,
    date: str,
    airline: str,
    class_filter: str,
    exclude_codeshares: bool = True,
) -> str:
    """Fare-class availability results. `date` is YYYY-MM-DD."""
    return f"{BASE}/air/availability/results?" + urlencode(
        {
            "origin": origin.upper(),
            "destination": destination.upper(),
            "departureDateTime": f"{date}T00:00",
            "alliance": "none",
            "airLineCodes": airline.upper(),
            "excludeCodeshares": str(bool(exclude_codeshares)).lower(),
            "classFilter": class_filter.upper(),
            "pcc": "USA (Default)",
            "resultsDisplay": "tabbed",
        }
    )


def status_url(airline: str, flight_number: str, date: str) -> str:
    """Flight status results — the hop that resolves a flight number to a route."""
    return f"{BASE}/air/status/results?" + urlencode(
        {
            "departureDateTime": date,
            "airlineCode": airline.upper(),
            "flightNumber": str(flight_number),
        }
    )


def seat_map_url(
    origin: str,
    destination: str,
    date: str,
    airline: str,
    flight_number: str,
    cabin: str,
) -> str:
    """Seat map results. `cabin` is a cabin code — W is Comfort+."""
    return f"{BASE}/air/seat-map/results?" + urlencode(
        {
            "departingAirport": origin.upper(),
            "arrivingAirport": destination.upper(),
            "departDate": date,
            "airline": airline.upper(),
            "flightNumber": str(flight_number),
            "paxID": "passenger1",
            "ptc": "ADT",
            "withRawXML": "true",
            "cabinClass": cabin.upper(),
        }
    )
