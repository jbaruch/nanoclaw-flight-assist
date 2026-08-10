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

Cabin outranks the exit row, per the operator: a better cabin buys
front-of-the-bus AND leg room, whereas an exit row buys leg room alone. Cabins
rank on the full ladder (`CABIN_SCORE`), not Comfort+-versus-everything: a
two-value split puts First and Delta One BELOW Comfort+, so a Comfort+ window
in row 30 reads as an upgrade from seat 1A.

Window-versus-row is graded rather than absolute: an aisle up front beats a
window far back, but a window a few rows further back still wins. WINDOW_WORTH_ROWS
is that exchange rate.
"""

from __future__ import annotations

import re

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

FIRST = "F"
BUSINESS = "C"
PREMIUM_ECONOMY = "A"
COMFORT_PLUS = "W"
MAIN_CABIN = "Y"

# The cabin ladder, higher is better. Codes are the service's, per
# `skills/expertflyer/references/web-contract.md`. Ranking Comfort+ against
# everything else instead would score First, Delta One and Premium Select at
# the Main Cabin's 0 — so a Comfort+ seat would outrank the First seat already
# held, which is the one comparison a seat check must never get backwards.
CABIN_SCORE = {MAIN_CABIN: 0, COMFORT_PLUS: 1, PREMIUM_ECONOMY: 2, BUSINESS: 3, FIRST: 4}

# The words the operator uses for each cabin, from the same table. Premium
# economy is Premium Select (`A`) and is NOT Comfort+ (`W`); resolving it to
# `W` would compare a seat against the wrong cabin's ladder position.
CABIN_ALIASES = {
    "first": FIRST,
    "business": BUSINESS,
    "delta one": BUSINESS,
    "premium economy": PREMIUM_ECONOMY,
    "premium select": PREMIUM_ECONOMY,
    "comfort+": COMFORT_PLUS,
    "comfort plus": COMFORT_PLUS,
    "economy comfort": COMFORT_PLUS,
    "economy": MAIN_CABIN,
    "main cabin": MAIN_CABIN,
    "coach": MAIN_CABIN,
}

# Exit tiers, rule 3. The recline distinction separates paired exit rows: the
# forward one is fixed-back precisely BECAUSE the second sits behind it.
EXIT_RECLINE = 2
EXIT_NO_RECLINE = 1
NOT_EXIT = 0


class SeatQualityError(ValueError):
    """A seat that cannot be ranked, rather than one ranked badly."""


def cabins_at_or_above(cabin: str, rungs: int) -> list[str]:
    """The held cabin plus the `rungs` cabins above it, best first.

    A seat check that reads only the cabin already occupied cannot see the
    Comfort+ window that opened while the operator sits in Main — the whole
    class of upgrade worth interrupting for. Walking the ladder is what makes
    that visible, and each rung costs one more request to a bot-walled
    service, so the width is the caller's to choose.
    """
    if rungs < 0:
        raise SeatQualityError(f"rungs must be zero or more, got {rungs}")
    held = CABIN_SCORE[cabin_code(cabin)]
    reachable = [c for c, score in CABIN_SCORE.items() if held <= score <= held + rungs]
    return sorted(reachable, key=lambda c: CABIN_SCORE[c], reverse=True)


def cabins_above(cabin: str) -> list[str]:
    """Every cabin that outranks this one, best first.

    What a sweep stopping at `cabin` did NOT look at. A verdict of "nothing
    open beats the held seat" is only ever true of the cabins actually read,
    so the ones left out have to be nameable.
    """
    score = CABIN_SCORE[cabin_code(cabin)]
    above = [c for c, other in CABIN_SCORE.items() if other > score]
    return sorted(above, key=lambda c: CABIN_SCORE[c], reverse=True)


# A seat designator is a row number followed by a column letter — "21F", "1A".
# Fully enumerable, unlike the free text elsewhere in a booking.
_LABEL_RE = re.compile(r"^(\d{1,3})\s*([A-Z]{1,2})$")


def parse_seat_label(label: str) -> tuple[int, str]:
    """Row and column from a seat designator, e.g. '21F' -> (21, 'F').

    Rows are numbered from 1. Row 0 parses as a number and then sorts ahead of
    row 1, so a mistyped seat would rank as the furthest-forward seat on the
    aircraft and report every real seat as worse than it.
    """
    match = _LABEL_RE.match(str(label).strip().upper())
    if not match:
        raise SeatQualityError(
            f"{label!r} is not a seat designator — pass a row and column like 21F or 1A"
        )
    row = int(match.group(1))
    if row < 1:
        raise SeatQualityError(
            f"{label!r} is row {row}, and seat rows start at 1 — pass the seat as printed "
            "on the boarding pass, e.g. 21F"
        )
    return row, match.group(2)


def column_positions(seats) -> dict[str, str]:
    """Column letter to position, read off the cabin's own bookable seats.

    The service reports open seats only, so the seat already held is never in
    the list and its position cannot be looked up directly. It can be derived:
    the aircraft states what column F is on every open seat in the same cabin.

    A column reported as two different positions is dropped rather than
    resolved — a cabin whose forward rows are 2-2 and rear rows 3-3 makes the
    same letter a window and a middle, and picking one would decide the
    operator is in a seat they are not in.
    """
    seen: dict[str, str | None] = {}
    for seat in seats:
        column = seat.get("column")
        if not column:
            continue
        position = seat.get("position")
        if position not in POSITION_SCORE and position not in EXCLUDED_POSITIONS:
            continue
        key = str(column).strip().upper()
        if key in seen and seen[key] != position:
            seen[key] = None
        else:
            seen[key] = str(position)
    return {column: position for column, position in seen.items() if position}


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
    if named is not None:
        # A present-but-unknown value and an absent one send the operator to
        # different places — one is a service that renamed a position, the
        # other is a seat map that classified nothing.
        raise SeatQualityError(
            f"seat {seat.get('label')!r} reports position {named!r}, which is not "
            f"window, aisle or middle — the service's vocabulary changed, so "
            f"ranking it would guess at what the operator would sit in"
        )
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


def cabin_code(cabin: str) -> str:
    """The service's cabin code, from a code or the words the operator uses.

    Both shapes reach ranking: the service stamps a code on each seat, while a
    held cabin arrives as whatever the operator said. An unrecognised cabin
    raises — scoring it as Main Cabin would silently rank a premium seat at the
    bottom of the ladder and report the downgrade as an upgrade.
    """
    text = str(cabin).strip()
    if text.upper() in CABIN_SCORE:
        return text.upper()
    code = CABIN_ALIASES.get(text.lower())
    if code is None:
        raise SeatQualityError(
            f"cabin {cabin!r} is not a cabin this ranks — pass one of "
            f"{', '.join(sorted(CABIN_SCORE))} or a name from "
            f"{', '.join(sorted(CABIN_ALIASES))}"
        )
    return code


def _cabin_rank(cabin: str) -> int:
    return CABIN_SCORE[cabin_code(cabin)]


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
    try:
        return cabin_code(cabin)
    except SeatQualityError as exc:
        # `cabin_code` knows the cabin but not whose it is. The seat label is
        # what the operator needs to act on, and this is the outermost place
        # that still has it, so the label is attached here.
        raise SeatQualityError(f"seat {seat.get('label')!r}: {exc}") from exc


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


def is_upgrade(
    candidate: dict,
    current: dict | None,
    cabin: str | None = None,
    cabin_exit_rows=None,
) -> bool:
    """True when `candidate` is strictly better than the seat already held.

    Used for the watch case: only worth interrupting the operator when the
    seat that opened actually beats what they have.

    `cabin_exit_rows` carries the same layout ranking uses. Without it a rear
    reclining exit row looks fixed-back and can lose to the forward row it
    should beat — the comparison this function exists to make.
    """
    if not is_acceptable(candidate):
        return False
    if current is None:
        return True
    if not is_acceptable(current):
        # The operator is in a middle. Rule 1 is absolute, so a middle has no
        # position score to sort by — and needs none: every seat worth taking
        # beats it, including one that would lose on cabin and on row.
        return True
    pair = [candidate, current]
    tiers = exit_tiers(pair, cabin_exit_rows)
    # Each seat resolves its OWN cabin: the candidate may be Comfort+ while the
    # held seat is Main, which is exactly the comparison worth making.
    return seat_sort_key(candidate, cabin, tiers) > seat_sort_key(current, cabin, tiers)
