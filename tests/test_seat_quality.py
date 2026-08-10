"""Pin the operator's seat preferences as stated.

Each test names the rule it encodes, so a future change to the ordering has
to argue with the rule rather than with a number.
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


sq = _load("seat_quality", "skills/expertflyer/scripts/seat_quality.py")

W, Y = sq.COMFORT_PLUS, sq.MAIN_CABIN
F, C, A = sq.FIRST, sq.BUSINESS, sq.PREMIUM_ECONOMY


def seat(row, label, position, *, exit_row=False, reclines=False, bulkhead=False):
    return {
        "row": row,
        "label": label,
        "isWindow": position == "window",
        "isAisle": position == "aisle",
        "isMiddle": position == "middle",
        "isExitRow": exit_row,
        "reclines": reclines,
        "isBulkhead": bulkhead,
    }


# --- rule 1: window > aisle, middle never ------------------------------------


def test_window_beats_aisle():
    window, aisle = seat(20, "A", "window"), seat(20, "C", "aisle")
    assert sq.rank_seats([aisle, window], W)[0] is window


def test_middle_is_excluded_not_merely_ranked_last():
    middle = seat(10, "B", "middle")
    assert sq.is_acceptable(middle) is False
    assert sq.rank_seats([middle], W) == []


def test_a_cabin_of_only_middles_yields_nothing():
    """DL2957 Comfort+: 13B and 14B open, both middles — offer nothing."""
    seats = [seat(13, "B", "middle"), seat(14, "B", "middle")]
    assert sq.rank_seats(seats, W) == []
    assert sq.best_seat(seats, W) is None


def test_main_cabin_non_middle_beats_comfort_plus_middle():
    """Rule 1, and rule 4 restated: the middle is not an option at all."""
    main_aisle = seat(30, "C", "aisle")
    assert sq.is_acceptable(main_aisle) is True
    assert sq.is_acceptable(seat(12, "B", "middle")) is False


# --- rule 2: closer to the front ---------------------------------------------


def test_closer_to_the_front_wins_among_equals():
    front, back = seat(10, "A", "window"), seat(30, "A", "window")
    assert sq.rank_seats([back, front], W)[0] is front


def test_bulkhead_carries_no_penalty():
    plain = seat(12, "A", "window")
    bulk = seat(12, "A", "window", bulkhead=True)
    assert sq.seat_sort_key(bulk, W) == sq.seat_sort_key(plain, W)


# --- rules 2 + 3: Main Cabin exit rows ---------------------------------------


def test_the_exit_row_beats_being_further_forward():
    forward = seat(15, "A", "window")
    exit_back = seat(30, "A", "window", exit_row=True)
    assert sq.rank_seats([forward, exit_back], Y)[0] is exit_back


def test_reclining_exit_row_beats_the_fixed_one():
    fixed = seat(30, "A", "window", exit_row=True, reclines=False)
    reclining = seat(31, "A", "window", exit_row=True, reclines=True)
    assert sq.rank_seats([fixed, reclining], Y)[0] is reclining


def test_exit_row_beats_any_other_main_seat_including_a_window():
    exit_aisle = seat(30, "C", "aisle", exit_row=True)
    plain_window = seat(12, "A", "window")
    assert sq.rank_seats([plain_window, exit_aisle], Y)[0] is exit_aisle


def test_exit_row_counts_in_comfort_plus_too():
    """Rule 5: harmless where W has no exit row, correct where it does."""
    exit_back = seat(30, "A", "window", exit_row=True)
    forward = seat(15, "A", "window")
    assert sq.rank_seats([forward, exit_back], W)[0] is exit_back


def test_unknown_recline_does_not_promote_a_possibly_fixed_seat():
    unknown = {**seat(30, "A", "window", exit_row=True)}
    del unknown["reclines"]
    known_reclining = seat(31, "A", "window", exit_row=True, reclines=True)
    assert sq.rank_seats([unknown, known_reclining], Y)[0] is known_reclining


# --- documented tie-breaks ---------------------------------------------------


def test_a_window_within_the_exchange_rate_beats_an_aisle_up_front():
    """ "in a span of 3 rows I'd take window further back"."""
    front_aisle = seat(10, "C", "aisle")
    window_3_back = seat(10 + sq.WINDOW_WORTH_ROWS, "A", "window")
    assert sq.rank_seats([front_aisle, window_3_back], W)[0] is window_3_back


def test_a_window_beyond_the_exchange_rate_loses_to_the_aisle():
    """ "Aisle up front beats window way back"."""
    front_aisle = seat(10, "C", "aisle")
    window_too_far = seat(10 + sq.WINDOW_WORTH_ROWS + 1, "A", "window")
    assert sq.rank_seats([window_too_far, front_aisle], W)[0] is front_aisle


def test_comfort_plus_outranks_an_exit_row():
    """Comfort+ buys forward position AND leg room; an exit row only leg room."""
    assert sq.CABIN_OUTRANKS_EXIT is True
    assert sq.seat_sort_key(seat(20, "C", "aisle"), W) > sq.seat_sort_key(
        seat(30, "A", "window", exit_row=True, reclines=True), Y
    )


# --- the watch case ----------------------------------------------------------


def test_only_a_strictly_better_seat_is_worth_interrupting_for():
    current = seat(20, "C", "aisle")
    assert sq.is_upgrade(seat(12, "A", "window"), current, W) is True
    assert sq.is_upgrade(seat(28, "C", "aisle"), current, W) is False
    assert sq.is_upgrade(current, current, W) is False


def test_a_middle_is_never_an_upgrade_even_from_nothing():
    assert sq.is_upgrade(seat(10, "B", "middle"), None, W) is False


def test_anything_acceptable_beats_having_no_seat_yet():
    assert sq.is_upgrade(seat(30, "C", "aisle"), None, W) is True


# --- failure mode ------------------------------------------------------------


def test_an_unclassified_seat_raises_rather_than_ranking_arbitrarily():
    with pytest.raises(sq.SeatQualityError):
        sq.is_acceptable({"row": 10, "label": "A"})


# --- rendering ---------------------------------------------------------------


def test_describe_names_what_makes_the_seat_good():
    assert sq.describe(seat(13, "A", "window"), W) == "13A (window)"
    assert "exit row, reclines" in sq.describe(
        seat(30, "C", "aisle", exit_row=True, reclines=True), Y
    )
    assert "bulkhead" in sq.describe(seat(12, "A", "window", bulkhead=True), W)


# --- the shape the service actually returns ----------------------------------


def test_ranks_the_service_response_shape_directly():
    """`/seats` reports `position` as a string, not isWindow/isAisle/isMiddle.

    Verbatim from the deployed service for DL2957 ATL-YYZ: one bookable seat,
    a middle in row 14. Ranking must consume this without translation.
    """
    live = [
        {
            "label": "14B",
            "row": 14,
            "column": "B",
            "position": "middle",
            "isExitRow": False,
            "isBulkhead": False,
            "cabin": "W",
        }
    ]
    assert sq.rank_seats(live, W) == []
    assert sq.best_seat(live, W) is None
    assert sq.is_upgrade(live[0], None, W) is False


def test_service_shape_window_and_aisle_rank_normally():
    seats = [
        {"label": "12C", "row": 12, "column": "C", "position": "aisle", "cabin": "W"},
        {"label": "14A", "row": 14, "column": "A", "position": "window", "cabin": "W"},
    ]
    assert sq.rank_seats(seats, W)[0]["label"] == "14A"


def test_an_unrecognised_position_string_raises_rather_than_guessing():
    with pytest.raises(sq.SeatQualityError):
        sq.is_acceptable({"row": 10, "label": "A", "position": "porthole"})


# --- review findings: cross-cabin comparison and label rendering -------------


def test_describe_does_not_double_the_row_on_a_service_label():
    """The service label is already row-qualified; naive concat renders 1414B."""
    live = {"label": "14B", "row": 14, "column": "B", "position": "middle", "cabin": W}
    assert sq.seat_label(live) == "14B"
    assert sq.describe(live).startswith("14B (")


def test_describe_still_composes_a_column_only_label():
    raw = {"label": "A", "row": 13, "position": "window", "cabin": W}
    assert sq.seat_label(raw) == "13A"


def test_upgrade_across_cabins_uses_each_seat_s_own_cabin():
    """Comfort+ outranks a Main exit row — unreachable with one shared cabin."""
    held_main_exit = {
        "label": "30A",
        "row": 30,
        "position": "window",
        "isExitRow": True,
        "reclines": True,
        "cabin": Y,
    }
    open_comfort = {"label": "20C", "row": 20, "position": "aisle", "cabin": W}
    assert sq.is_upgrade(open_comfort, held_main_exit) is True
    assert sq.is_upgrade(held_main_exit, open_comfort) is False


def test_a_worse_seat_in_a_better_cabin_still_wins():
    """Consequence of CABIN_OUTRANKS_EXIT, pinned so a flip is deliberate."""
    held = {"label": "12A", "row": 12, "position": "window", "cabin": Y}
    candidate = {"label": "40C", "row": 40, "position": "aisle", "cabin": W}
    assert sq.is_upgrade(candidate, held) is True


def test_a_seat_without_a_cabin_raises_rather_than_being_guessed():
    with pytest.raises(sq.SeatQualityError):
        sq.seat_sort_key({"label": "12A", "row": 12, "position": "window"})


def test_an_explicit_cabin_still_serves_as_the_fallback():
    seatless = {"label": "12A", "row": 12, "position": "window"}
    assert sq.seat_cabin(seatless, W) == W
    assert sq.rank_seats([seatless], W)[0] is seatless


# --- #249: recline derived from exit-row adjacency ---------------------------


def exit_seat(row, label="A", position="window", cabin=None):
    return {
        "label": f"{row}{label}",
        "row": row,
        "column": label,
        "position": position,
        "isExitRow": True,
        "cabin": cabin or W,
    }


def test_paired_exit_rows_put_the_second_ahead_of_the_first():
    """ "we don't want first, and want second" — the first cannot recline."""
    first, second = exit_seat(20), exit_seat(21)
    assert sq.rank_seats([first, second], W, [20, 21])[0] is second


def test_a_lone_exit_row_reclines():
    """Nothing behind it in the CABIN LAYOUT, so nothing stops it reclining."""
    lone = exit_seat(30)
    assert sq.exit_tiers([lone], [30]) == {30: sq.EXIT_RECLINE}


def test_only_the_row_with_an_exit_behind_it_is_fixed():
    seats = [exit_seat(20), exit_seat(21)]
    assert sq.exit_tiers(seats, [20, 21]) == {20: sq.EXIT_NO_RECLINE, 21: sq.EXIT_RECLINE}


def test_three_consecutive_exit_rows_fix_all_but_the_last():
    seats = [exit_seat(20), exit_seat(21), exit_seat(22)]
    tiers = sq.exit_tiers(seats, [20, 21, 22])
    assert tiers == {
        20: sq.EXIT_NO_RECLINE,
        21: sq.EXIT_NO_RECLINE,
        22: sq.EXIT_RECLINE,
    }


def test_non_adjacent_exit_rows_both_recline():
    """The adjacency rule must not over-fire on separated exit rows."""
    seats = [exit_seat(12), exit_seat(30)]
    assert sq.exit_tiers(seats, [12, 30]) == {12: sq.EXIT_RECLINE, 30: sq.EXIT_RECLINE}
    # Both recline, so the tie falls to the ordinary rule: further forward.
    assert sq.rank_seats(seats, W, [12, 30])[0]["row"] == 12


def test_adjacency_is_computed_over_the_whole_cabin_including_dropped_seats():
    """A middle in the row behind still fixes the row in front."""
    seats = [
        exit_seat(20),
        {
            "label": "21B",
            "row": 21,
            "column": "B",
            "position": "middle",
            "isExitRow": True,
            "cabin": W,
        },
    ]
    assert sq.exit_tiers(seats, [20, 21])[20] == sq.EXIT_NO_RECLINE
    assert [s["row"] for s in sq.rank_seats(seats, W, [20, 21])] == [20]


def test_describe_names_the_reclining_exit_row_from_adjacency():
    seats = [exit_seat(20), exit_seat(21)]
    tiers = sq.exit_tiers(seats, [20, 21])
    assert "exit row, reclines" in sq.describe(seats[1], W, tiers)
    assert sq.describe(seats[0], W, tiers).endswith("exit row)")


def test_an_isolated_seat_still_falls_back_to_the_reclines_field():
    """No cabin context: honour an explicit flag rather than guessing."""
    solo = {**exit_seat(30), "reclines": True}
    assert sq._exit_tier(solo, W) == sq.EXIT_RECLINE
    del solo["reclines"]
    assert sq._exit_tier(solo, W) == sq.EXIT_NO_RECLINE


def test_an_occupied_rear_exit_row_must_not_promote_the_row_in_front():
    """The service lists bookable seats only.

    Row 21 is an exit row but fully occupied, so it never appears in `seats`.
    Deriving adjacency from the bookable list alone would call row 20
    reclining — recommending precisely the fixed-back seat the operator does
    not want. The cabin layout is what settles it.
    """
    bookable = [exit_seat(20)]
    layout = [20, 21]
    assert sq.exit_tiers(bookable, layout) == {20: sq.EXIT_NO_RECLINE}
    assert sq.describe(bookable[0], W, sq.exit_tiers(bookable, layout)).endswith("exit row)")


def test_without_a_layout_no_exit_row_is_claimed_to_recline():
    """Conservative default: never promote a seat that may be fixed-back."""
    assert sq.exit_tiers([exit_seat(30)]) == {30: sq.EXIT_NO_RECLINE}
    assert sq.exit_tiers([exit_seat(20), exit_seat(21)]) == {
        20: sq.EXIT_NO_RECLINE,
        21: sq.EXIT_NO_RECLINE,
    }


def test_upgrade_sees_the_reclining_exit_row_when_given_the_layout():
    """The watch case: 21A opens and must beat the fixed-back 20A held today."""
    held = exit_seat(20, cabin=Y)
    opened = exit_seat(21, cabin=Y)
    assert sq.is_upgrade(opened, held, Y, [20, 21]) is True
    assert sq.is_upgrade(held, opened, Y, [20, 21]) is False


def test_upgrade_without_a_layout_does_not_invent_a_recline():
    """Neither is claimed to recline, so the forward row wins on position."""
    held = exit_seat(20, cabin=Y)
    opened = exit_seat(21, cabin=Y)
    assert sq.is_upgrade(opened, held, Y) is False


# --- the cabin ladder --------------------------------------------------------


def test_cabins_rank_on_the_full_ladder_not_comfort_plus_versus_the_rest():
    """First > Delta One > Premium Select > Comfort+ > Main Cabin.

    Scoring only Comfort+ above zero put every premium cabin level with Main,
    so a Comfort+ seat outranked the First seat already held.
    """
    ladder = [F, C, A, W, Y]
    scores = [sq.CABIN_SCORE[code] for code in ladder]
    assert scores == sorted(scores, reverse=True)
    assert len(set(scores)) == len(ladder)


def test_a_comfort_plus_window_does_not_beat_the_first_seat_already_held():
    """The live case: 1A on DL2714, with Comfort+ windows open further back."""
    held_first = {"label": "1A", "row": 1, "position": "window", "cabin": F}
    open_comfort = {"label": "10A", "row": 10, "position": "window", "cabin": W}
    assert sq.is_upgrade(open_comfort, held_first) is False
    assert sq.is_upgrade(held_first, open_comfort) is True


def test_every_premium_cabin_outranks_comfort_plus():
    comfort_window = {"label": "10A", "row": 10, "position": "window", "cabin": W}
    for better in (A, C, F):
        far_back_aisle = {"label": "40C", "row": 40, "position": "aisle", "cabin": better}
        assert sq.is_upgrade(far_back_aisle, comfort_window) is True


def test_premium_economy_is_premium_select_never_comfort_plus():
    """`A` and `W` are different cabins that both get called 'premium'."""
    assert sq.cabin_code("premium economy") == A
    assert sq.cabin_code("premium select") == A
    assert sq.cabin_code("comfort+") == W
    assert sq.CABIN_SCORE[A] > sq.CABIN_SCORE[W]


def test_the_operator_s_words_resolve_to_the_service_s_codes():
    assert sq.cabin_code("Delta One") == C
    assert sq.cabin_code("main cabin") == Y
    assert sq.cabin_code("coach") == Y
    # A code passes through whatever its case.
    assert sq.cabin_code("w") == W
    assert sq.cabin_code(" F ") == F


def test_an_unknown_cabin_raises_rather_than_scoring_as_main():
    """Scoring an unknown cabin at 0 reports a downgrade as an upgrade."""
    with pytest.raises(sq.SeatQualityError):
        sq.cabin_code("sky club")
    with pytest.raises(sq.SeatQualityError):
        sq.seat_sort_key({"label": "2A", "row": 2, "position": "window", "cabin": "P"})


def test_an_unknown_cabin_names_the_seat_it_came_from():
    """Step 5 relays `detail` and promises it names the seat, so a cabin fault
    has to carry the label too — `cabin_code` alone does not know it."""
    with pytest.raises(sq.SeatQualityError, match="2A"):
        sq.seat_cabin({"label": "2A", "row": 2, "position": "window", "cabin": "P"})
    with pytest.raises(sq.SeatQualityError, match="2A"):
        sq.seat_sort_key({"label": "2A", "row": 2, "position": "window", "cabin": "P"})


def test_a_seat_cabin_resolves_to_the_service_code():
    """Prose on the seat normalises, so ranking compares codes throughout."""
    assert sq.seat_cabin({"label": "2A", "cabin": "Delta One"}) == C
    assert sq.seat_cabin({"label": "2A"}, "premium select") == A


# --- the cabin sweep and the held seat ---------------------------------------


def test_the_sweep_walks_up_the_ladder_from_the_held_cabin():
    """A check that reads only the occupied cabin cannot see the Comfort+ seat
    that opened while the operator sits in Main."""
    assert sq.cabins_at_or_above(Y, 1) == [W, Y]
    assert sq.cabins_at_or_above(Y, 0) == [Y]
    assert sq.cabins_at_or_above(W, 2) == [C, A, W]


def test_the_sweep_stops_at_the_top_of_the_ladder():
    """Nothing sits above First, however many rungs are asked for."""
    assert sq.cabins_at_or_above(F, 3) == [F]
    assert sq.cabins_at_or_above(C, 5) == [F, C]


def test_the_sweep_rejects_a_negative_width():
    with pytest.raises(sq.SeatQualityError):
        sq.cabins_at_or_above(Y, -1)


def test_a_seat_designator_splits_into_row_and_column():
    assert sq.parse_seat_label("21F") == (21, "F")
    assert sq.parse_seat_label("1A") == (1, "A")
    assert sq.parse_seat_label(" 10c ") == (10, "C")


def test_something_that_is_not_a_seat_designator_raises():
    for bad in ("F21", "21", "A", "", "21FF9", "row 21"):
        with pytest.raises(sq.SeatQualityError):
            sq.parse_seat_label(bad)


def test_row_zero_is_rejected_rather_than_ranked_ahead_of_row_one():
    """Row 0 parses as a number and sorts ahead of every real row, so a
    mistyped seat would report the whole aircraft as worse than it."""
    for bad in ("0A", "00C", "0F"):
        with pytest.raises(sq.SeatQualityError, match="start at 1"):
            sq.parse_seat_label(bad)
    assert sq.parse_seat_label("1A") == (1, "A")


def test_the_held_seat_s_position_is_read_off_the_open_seats_beside_it():
    """The held seat is occupied, so it is never in the service's response.
    The aircraft still states what column F is, on every open seat."""
    open_seats = [
        {"label": "12A", "row": 12, "column": "A", "position": "window"},
        {"label": "14B", "row": 14, "column": "B", "position": "middle"},
        {"label": "15F", "row": 15, "column": "F", "position": "window"},
    ]
    assert sq.column_positions(open_seats) == {"A": "window", "B": "middle", "F": "window"}


def test_a_column_reported_two_ways_is_dropped_rather_than_picked():
    """Forward rows 2-2 and rear rows 3-3 make one letter both a window and a
    middle; choosing one decides the operator is in a seat they are not in."""
    conflicting = [
        {"label": "2C", "row": 2, "column": "C", "position": "window"},
        {"label": "20C", "row": 20, "column": "C", "position": "aisle"},
        {"label": "20A", "row": 20, "column": "A", "position": "window"},
    ]
    assert sq.column_positions(conflicting) == {"A": "window"}


def test_columns_ignore_seats_with_no_usable_position():
    unusable = [
        {"label": "12A", "row": 12, "column": "A", "position": "porthole"},
        {"label": "12D", "row": 12, "position": "aisle"},
        {"label": "12C", "row": 12, "column": "c", "position": "aisle"},
    ]
    assert sq.column_positions(unusable) == {"C": "aisle"}


def test_anything_worth_taking_beats_a_middle_already_held():
    """Rule 1 is absolute, so a held middle has no position score to sort by.
    It needs none: every acceptable seat beats it, however far back."""
    held_middle = {"label": "13B", "row": 13, "position": "middle", "cabin": W}
    assert sq.is_upgrade({"label": "20C", "row": 20, "position": "aisle", "cabin": W}, held_middle)
    # Worse cabin, worse row, still an upgrade — a middle is never taken.
    assert sq.is_upgrade({"label": "40C", "row": 40, "position": "aisle", "cabin": Y}, held_middle)
    # Another middle is not.
    assert (
        sq.is_upgrade({"label": "40B", "row": 40, "position": "middle", "cabin": Y}, held_middle)
        is False
    )
