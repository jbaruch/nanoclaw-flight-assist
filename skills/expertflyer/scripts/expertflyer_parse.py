"""Pure parsing and decision helpers for the ExpertFlyer skill.

No browser, no network — everything here is a pure function so the semantics
that are easy to get backwards (seat state vs decoration, capped inventory,
alert-worthiness) are pinned by tests rather than by a comment.

See ../references/web-contract.md for where these rules come from.
"""

from __future__ import annotations

import re
from urllib.parse import urlencode

BASE = "https://www.expertflyer.com"

# A seat's STATE decides availability. Everything else is decoration that
# renders on top of a seat in any state — a grey "wing" cell with no occupant
# is bookable, and reading it as unavailable hides open seats.
STATE_MARKS = frozenset({"occupied", "blocked"})
DECORATION_MARKS = frozenset(
    {
        "wing",
        "exit_row",
        "paid",
        "premium",
        "paid_premium",
        "accessible",
        "selected",
        "highlighted",
    }
)

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


class UnknownSeatMark(ValueError):
    """A mark the legend does not cover.

    Raised rather than ignored: an unrecognised mark could be a new STATE, and
    silently filing it under decoration would report an unavailable seat as
    bookable.
    """


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


def classify_seat(marks) -> str:
    """Return 'occupied', 'blocked' or 'available' for one seat's marks."""
    unknown = set(marks) - STATE_MARKS - DECORATION_MARKS
    if unknown:
        raise UnknownSeatMark(
            f"unrecognised seat mark(s) {sorted(unknown)} — classify them in "
            "STATE_MARKS or DECORATION_MARKS before trusting this seat map"
        )
    if "blocked" in marks:
        return "blocked"
    if "occupied" in marks:
        return "occupied"
    return "available"


def seat_position(column: str, layout) -> str:
    """Classify a seat column as 'window', 'aisle' or 'middle'.

    `layout` is the cabin's column groups, e.g. ["ABC", "DEF"] for 3-3 or
    ["AB", "DE", "FG"] for a widebody. Window seats are the outer edges of the
    outer groups; aisle seats border a gap; the rest are middles.
    """
    groups = [g.upper() for g in layout if g]
    if not groups:
        raise ValueError("layout must contain at least one column group")
    column = column.upper()

    windows, aisles = set(), set()
    for i, group in enumerate(groups):
        first_group, last_group = i == 0, i == len(groups) - 1
        (windows if first_group else aisles).add(group[0])
        (windows if last_group else aisles).add(group[-1])

    if column in windows:
        return "window"
    if column in aisles:
        return "aisle"
    if any(column in g for g in groups):
        return "middle"
    raise ValueError(f"column {column!r} is not in layout {layout!r}")


def matching_seats(seats, wants, layout) -> list[str]:
    """Free seats whose position matches any wanted position.

    `seats` is an iterable of {"row": int, "column": str, "marks": [...]}.
    `wants` is e.g. ("aisle", "window") — "non-middle" expands to both.
    """
    wanted = {w.lower() for w in wants}
    unknown = wanted - {"window", "aisle", "middle"}
    if unknown:
        raise ValueError(f"unknown seat position(s) {sorted(unknown)}")
    out = []
    for seat in seats:
        if classify_seat(seat.get("marks", ())) != "available":
            continue
        if seat_position(seat["column"], layout) in wanted:
            out.append(f"{seat['row']}{seat['column'].upper()}")
    return out


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


def recommend_alert(matches) -> bool:
    """An alert is worth creating only when nothing wanted is already free."""
    return not matches


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
            "withRawXML": "false",
            "cabinClass": cabin.upper(),
        }
    )
