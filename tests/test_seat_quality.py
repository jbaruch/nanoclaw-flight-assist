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
