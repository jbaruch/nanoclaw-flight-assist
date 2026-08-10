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
