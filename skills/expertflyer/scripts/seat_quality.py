"""Rank open seats by the operator's stated preferences.

Ranking is preference, not fact, so it lives here rather than in the
`expertflyer-api` service: the service reports what a seat *is*, this decides
what a seat is *worth*.

The operator's rules, verbatim in effect:

  1. Window best, aisle works, middle never. A Main Cabin non-middle beats a
     Comfort+ middle — and since middles are never taken, a middle is not
     ranked low, it is excluded outright.
  2. Closer to the front is better. Bulkhead carries no penalty. An
     emergency exit row beats being further forward.
  3. Exit row WITH recline (the second one) beats exit row without recline,
     which beats any other seat in the cabin.
  4. A Comfort+ middle never beats a Main Cabin aisle.
  5. The exit-row preference applies in every cabin. Exit rows are
     overwhelmingly a Main Cabin feature, but the preference is harmless
     where there are none and correct where there are.

Cabin outranks the exit row, per the operator: Comfort+ buys front-of-the-bus
AND leg room, whereas an exit row buys leg room alone.

Window-versus-row is graded rather than absolute: an aisle up front beats a
window far back, but a window a few rows further back still wins. WINDOW_WORTH_ROWS
is that exchange rate.
"""

from __future__ import annotations

# Comfort+ beats an exit row: it buys forward position AND leg room, where an
# exit row buys only leg room.
CABIN_OUTRANKS_EXIT = True

# How many rows further back a window may sit and still beat an aisle. At
# exactly this many rows the window wins; beyond it, the aisle does.
WINDOW_WORTH_ROWS = 3

# Never offered. Rule 1 is absolute: "middle - never".
EXCLUDED_POSITIONS = frozenset({"middle"})

# Higher is better.
POSITION_SCORE = {"window": 2, "aisle": 1}

COMFORT_PLUS = "W"
MAIN_CABIN = "Y"

# Main Cabin exit tiers, rule 3. The recline distinction is what separates
# the two exit rows on most narrowbodies — the first is fixed-back.
EXIT_RECLINE = 2
EXIT_NO_RECLINE = 1
NOT_EXIT = 0


class SeatQualityError(ValueError):
    """A seat that cannot be ranked, rather than one ranked badly."""


def is_acceptable(seat: dict) -> bool:
    """False for seats the operator would never take, whatever else is true."""
    return _position(seat) not in EXCLUDED_POSITIONS


def _position(seat: dict) -> str:
    """Seat position, from whichever shape the caller supplies.

    The expertflyer-api service reports a `position` string; the raw seat-map
    payload it parses uses isWindow/isAisle/isMiddle booleans. Accept both, so
    the ranker works against the API response and against a captured fixture
    without a translation layer between them.
    """
    named = seat.get("position")
    if named in POSITION_SCORE or named in EXCLUDED_POSITIONS:
        return str(named)
    if seat.get("isWindow"):
        return "window"
    if seat.get("isAisle"):
        return "aisle"
    if seat.get("isMiddle"):
        return "middle"
    raise SeatQualityError(
        f"seat {seat.get('label')!r} reports no window/aisle/middle flag — "
        "the seat map did not classify it, so it cannot be ranked"
    )


def _exit_tier(seat: dict, cabin: str) -> int:
    """Exit-row tier, applied in every cabin.

    Exit rows are overwhelmingly a Main Cabin feature, but scoping the
    preference to Main would silently mis-rank a cabin that happens to have
    one. `cabin` is retained for callers and future per-cabin nuance.
    """
    if not seat.get("isExitRow"):
        return NOT_EXIT
    # `reclines` absent means unknown; treat as the weaker exit tier rather
    # than promoting a seat that may be fixed-back.
    return EXIT_RECLINE if seat.get("reclines") else EXIT_NO_RECLINE


def _cabin_rank(cabin: str) -> int:
    return 1 if cabin == COMFORT_PLUS else 0


def seat_sort_key(seat: dict, cabin: str) -> tuple:
    """Sort key for one seat; higher tuples are better seats.

    Position and row trade off rather than one dominating: a window counts as
    WINDOW_WORTH_ROWS rows further forward than it is. Row is negated so a
    lower row number sorts higher. The raw position breaks exact ties, which
    is what makes a window exactly WINDOW_WORTH_ROWS back beat the aisle.
    """
    position = POSITION_SCORE[_position(seat)]
    exit_tier = _exit_tier(seat, cabin)
    row = int(seat["row"])

    effective_row = row - WINDOW_WORTH_ROWS if _position(seat) == "window" else row
    cabin_key = _cabin_rank(cabin) if CABIN_OUTRANKS_EXIT else 0
    return (cabin_key, exit_tier, -effective_row, position)


def rank_seats(seats, cabin: str) -> list[dict]:
    """Acceptable seats, best first. Middles are dropped, never ranked last."""
    usable = [s for s in seats if is_acceptable(s)]
    return sorted(usable, key=lambda s: seat_sort_key(s, cabin), reverse=True)


def best_seat(seats, cabin: str) -> dict | None:
    ranked = rank_seats(seats, cabin)
    return ranked[0] if ranked else None


def describe(seat: dict, cabin: str) -> str:
    """Short human label, e.g. '13A (window, exit row)'."""
    bits = [_position(seat)]
    tier = _exit_tier(seat, cabin)
    if tier == EXIT_RECLINE:
        bits.append("exit row, reclines")
    elif tier == EXIT_NO_RECLINE:
        bits.append("exit row")
    if seat.get("isBulkhead"):
        bits.append("bulkhead")
    return f"{seat['row']}{seat['label']} ({', '.join(bits)})"


def is_upgrade(candidate: dict, current: dict | None, cabin: str) -> bool:
    """True when `candidate` is strictly better than the seat already held.

    Used for the watch case: only worth interrupting the operator when the
    seat that opened actually beats what they have.
    """
    if not is_acceptable(candidate):
        return False
    if current is None:
        return True
    return seat_sort_key(candidate, cabin) > seat_sort_key(current, cabin)
