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

# Exit tiers, rule 3. The recline distinction separates paired exit rows: the
# forward one is fixed-back precisely BECAUSE the second sits behind it.
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


def exit_tiers(seats, cabin_exit_rows=None) -> dict[int, int]:
    """Map each exit row to its recline tier, derived from the cabin's layout.

    The seat map carries `isExitRow` and `row` but no recline signal, so the
    distinction is read off geometry: an exit row cannot recline when another
    exit row sits directly behind it. That is why the forward row of a pair is
    fixed-back, and why the second is the one worth having.

    `cabin_exit_rows` must be EVERY exit row in the cabin. `seats` alone is not
    enough: the service reports bookable seats only, so an occupied rear exit
    row is invisible and the open row in front of it would be promoted to
    reclining — recommending the one seat the operator specifically does not
    want. Without the full layout, no exit row is claimed to recline.
    """
    exits = [s for s in seats if s.get("isExitRow")]
    present = {int(s["row"]) for s in exits}
    if cabin_exit_rows is None:
        # No layout: fall back to an explicit per-seat flag where one exists,
        # and otherwise claim nothing. Never promote on absent evidence.
        return {
            int(s["row"]): (EXIT_RECLINE if s.get("reclines") else EXIT_NO_RECLINE) for s in exits
        }
    layout = {int(r) for r in cabin_exit_rows}
    return {r: (EXIT_NO_RECLINE if (r + 1) in layout else EXIT_RECLINE) for r in present}


def _exit_tier(seat: dict, cabin: str, tiers: dict[int, int] | None = None) -> int:
    """Exit-row tier, applied in every cabin.

    Exit rows are overwhelmingly a Main Cabin feature, but scoping the
    preference to Main would silently mis-rank a cabin that happens to have
    one. `cabin` is retained for callers and future per-cabin nuance.

    `tiers` comes from `exit_tiers()` over the whole cabin. Without it — a
    single seat judged in isolation — fall back to an explicit `reclines`
    field, and failing that to the weaker tier, so an unknown seat is never
    promoted over one known to recline.
    """
    if not seat.get("isExitRow"):
        return NOT_EXIT
    if tiers is not None:
        return tiers.get(int(seat["row"]), EXIT_NO_RECLINE)
    return EXIT_RECLINE if seat.get("reclines") else EXIT_NO_RECLINE


def _cabin_rank(cabin: str) -> int:
    return 1 if cabin == COMFORT_PLUS else 0


def seat_cabin(seat: dict, fallback: str | None = None) -> str:
    """The seat's own cabin, falling back to the caller's.

    The service stamps `cabin` on each seat. Comparing two seats therefore has
    to read each one's own cabin — a single shared argument cannot express
    "Comfort+ outranks a Main Cabin exit row", which is the rule that made
    cross-cabin comparison worth having.
    """
    cabin = seat.get("cabin") or fallback
    if not cabin:
        raise SeatQualityError(
            f"seat {seat.get('label')!r} carries no cabin and none was supplied — "
            "cabin decides rank, so guessing it would silently mis-order"
        )
    return str(cabin)


def seat_sort_key(
    seat: dict, cabin: str | None = None, tiers: dict[int, int] | None = None
) -> tuple:
    """Sort key for one seat; higher tuples are better seats.

    Position and row trade off rather than one dominating: a window counts as
    WINDOW_WORTH_ROWS rows further forward than it is. Row is negated so a
    lower row number sorts higher. The raw position breaks exact ties, which
    is what makes a window exactly WINDOW_WORTH_ROWS back beat the aisle.
    """
    resolved = seat_cabin(seat, cabin)
    position = POSITION_SCORE[_position(seat)]
    exit_tier = _exit_tier(seat, resolved, tiers)
    row = int(seat["row"])

    effective_row = row - WINDOW_WORTH_ROWS if _position(seat) == "window" else row
    cabin_key = _cabin_rank(resolved) if CABIN_OUTRANKS_EXIT else 0
    return (cabin_key, exit_tier, -effective_row, position)


def rank_seats(seats, cabin: str | None = None, cabin_exit_rows=None) -> list[dict]:
    """Acceptable seats, best first. Middles are dropped, never ranked last.

    `cabin_exit_rows` is the cabin's full exit-row set; see `exit_tiers`.
    """
    usable = [s for s in seats if is_acceptable(s)]
    # Adjacency is a property of the cabin, not of one seat, so the tiers are
    # computed over every seat given — including the ones ranking will drop.
    tiers = exit_tiers(seats, cabin_exit_rows)
    return sorted(usable, key=lambda s: seat_sort_key(s, cabin, tiers), reverse=True)


def best_seat(seats, cabin: str | None = None, cabin_exit_rows=None) -> dict | None:
    ranked = rank_seats(seats, cabin, cabin_exit_rows)
    return ranked[0] if ranked else None


def seat_label(seat: dict) -> str:
    """Full seat designator, whichever shape the caller supplies.

    The service reports `label` already row-qualified ("14B"); the raw
    seat-map payload reports the column alone ("B") with `row` beside it.
    Concatenating blindly renders "1414B".
    """
    label = str(seat.get("label", ""))
    row = str(seat.get("row", ""))
    if label.startswith(row) and label != row:
        return label
    column = seat.get("column") or label
    return f"{row}{column}"


def describe(seat: dict, cabin: str | None = None, tiers: dict[int, int] | None = None) -> str:
    """Short human label, e.g. '13A (window, exit row)'."""
    bits = [_position(seat)]
    tier = _exit_tier(seat, seat_cabin(seat, cabin), tiers)
    if tier == EXIT_RECLINE:
        bits.append("exit row, reclines")
    elif tier == EXIT_NO_RECLINE:
        bits.append("exit row")
    if seat.get("isBulkhead"):
        bits.append("bulkhead")
    return f"{seat_label(seat)} ({', '.join(bits)})"


def is_upgrade(candidate: dict, current: dict | None, cabin: str | None = None) -> bool:
    """True when `candidate` is strictly better than the seat already held.

    Used for the watch case: only worth interrupting the operator when the
    seat that opened actually beats what they have.
    """
    if not is_acceptable(candidate):
        return False
    if current is None:
        return True
    # Each seat resolves its OWN cabin: the candidate may be Comfort+ while the
    # held seat is Main, which is exactly the comparison worth making.
    return seat_sort_key(candidate, cabin) > seat_sort_key(current, cabin)
