"""Tests for nightly-travel-sync/scripts/tripit_local_time.py.

Locks down the documented contract per `coding-policy: testing-standards`:

  - `offset_minutes` returns the single UTC offset in [-12:00, +14:00] that
    maps a known UTC instant onto a printed wall clock, and None when no
    candidate or more than one candidate qualifies
  - `local_times` reads the departure clock printed above a record's
    `[Type]` marker and the arrival clock printed above its `Arrive` line,
    resolving each half against its own instant
  - Neither half ever inherits the other's offset: a record with one
    location and one clock (lodging, car rental) gets a start stamp and no
    end stamp
  - Every unresolvable shape fails closed (None), never a guessed date

Fixtures are the DESCRIPTION bodies TripIt actually emits, escapes included
(`\\n` line breaks, `\\,` commas), so the parser is exercised against the
raw field rather than a pre-cleaned copy.
"""

from datetime import datetime, timedelta, timezone

import pytest


def _utc(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


# The red-eye that started this: 11:05 PM PDT on Aug 22 is an Aug 23 UTC
# instant, and TripIt prints no date header above the departure clock.
RED_EYE = (
    "View and/or edit details in TripIt : https://www.tripit.com/trip/show/id/384031073\\n \\n\\n"
    "11:05 PM PDT\\n[Flight] SFO to BNA\\n \\n\\nSouthwest Airlines 1683\\, Terminal \\, Gate \\n"
    " \\n\\n \\nSun\\, Aug 23\\n5:30 AM CDT\\nArrive Nashville (BNA)\\nTerminal \\, Gate \\n"
)

# A daytime segment across two zones, departure clock under a date header.
DAY_HOP = (
    "View and/or edit details in TripIt : https://www.tripit.com/trip/show/id/384031073\\n \\n\\n"
    "Mon\\, Aug 17\\n6:20 AM CDT\\n[Flight] BNA to LAX\\n \\n\\nDelta Air Lines 891\\n \\n\\n"
    "8:30 AM PDT\\nArrive Los Angeles (LAX)\\n"
)

# One location, one clock. `Arrive` sits INSIDE the marker line, so it must
# not be read as a second zone.
CHECK_IN = (
    "View and/or edit details in TripIt : https://www.tripit.com/trip/show/id/384031073\\n \\n\\n"
    "3:00 PM PDT\\n[Lodging] Arrive Residence Inn by Marriott Palo Alto\\n"
    "Check-In: 3:00pm\\n1854 W El Camino Real\\, Mountain View\\, CA\\n"
)


@pytest.fixture
def tripit_local_time():
    import importlib.util
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[1]
        / "skills/nightly-travel-sync/scripts/tripit_local_time.py"
    )
    spec = importlib.util.spec_from_file_location("tripit_local_time_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- offset_minutes ---------------------------------------------------------


@pytest.mark.parametrize(
    ("instant", "hour", "minute", "expected"),
    [
        # 06:05Z printed as 11:05 PM → the clock ran back a day: -7h.
        (_utc(2026, 8, 23, 6, 5), 23, 5, -7 * 60),
        (_utc(2026, 8, 17, 11, 20), 6, 20, -5 * 60),
        # Ahead of UTC, and across midnight the other way.
        (_utc(2026, 9, 1, 7, 20), 9, 20, 2 * 60),
        (_utc(2026, 9, 1, 23, 0), 9, 0, 10 * 60),
        # Half- and quarter-hour offsets survive the minute arithmetic.
        (_utc(2026, 9, 1, 6, 0), 11, 30, 5 * 60 + 30),
        (_utc(2026, 9, 1, 6, 0), 11, 45, 5 * 60 + 45),
    ],
)
def test_offset_minutes_resolves_single_candidate(
    tripit_local_time, instant, hour, minute, expected
):
    assert tripit_local_time.offset_minutes(instant, hour, minute) == expected


def test_offset_minutes_refuses_the_ambiguous_pacific_band(tripit_local_time):
    """The inhabited offset range spans 26 hours, so a clock 11 hours behind
    UTC is equally consistent with +13:00 a day over. Two candidates is a
    refusal, never a coin flip."""
    assert tripit_local_time.offset_minutes(_utc(2026, 9, 1, 12, 0), 1, 0) is None


def test_a_resolved_offset_reproduces_the_printed_clock(tripit_local_time):
    """Sweep every minute-of-day pairing at 5-minute resolution: an offset
    this function returns must map the instant back onto the clock it was
    given. The accepted range is wider than a day, so there is always at
    least one candidate — a None here is the two-candidate refusal, never
    "no offset explains this"."""
    step = 5
    instant = _utc(2026, 9, 1, 6, 0)
    refusals = 0
    for total in range(0, 24 * 60, step):
        hour, minute = divmod(total, 60)
        offset = tripit_local_time.offset_minutes(instant, hour, minute)
        if offset is None:
            refusals += 1
            continue
        shifted = instant + timedelta(minutes=offset)
        assert (shifted.hour, shifted.minute) == (hour, minute)
    # The ambiguous band is exactly the range's overhang past 24 hours: the
    # accepted offsets span 26 hours, so two hours of clock values resolve
    # twice and nothing else does.
    overhang = tripit_local_time.MAX_OFFSET_MINUTES - tripit_local_time.MIN_OFFSET_MINUTES - 24 * 60
    assert 0 < refusals * step <= overhang + step


# --- local_times ------------------------------------------------------------


def test_red_eye_departure_keeps_the_evening_it_left(tripit_local_time):
    """The bug in one assertion: the UTC instant is Aug 23, the traveller
    boarded on Aug 22, and the local stamp has to say Aug 22."""
    start, end = tripit_local_time.local_times(
        _utc(2026, 8, 23, 6, 5), _utc(2026, 8, 23, 10, 30), RED_EYE
    )
    assert start is not None and end is not None
    assert start.isoformat() == "2026-08-22T23:05:00-07:00"
    assert start.date().isoformat() == "2026-08-22"
    assert end.isoformat() == "2026-08-23T05:30:00-05:00"


def test_local_times_preserve_the_instants(tripit_local_time):
    """A local stamp re-renders an instant; it never moves one."""
    start_utc, end_utc = _utc(2026, 8, 23, 6, 5), _utc(2026, 8, 23, 10, 30)
    start, end = tripit_local_time.local_times(start_utc, end_utc, RED_EYE)
    assert start == start_utc
    assert end == end_utc


def test_arrival_resolves_in_its_own_zone(tripit_local_time):
    """Each half of a segment reads its own printed clock, so a flight that
    lands two zones over reports the destination's offset, not the origin's."""
    start, end = tripit_local_time.local_times(
        _utc(2026, 8, 17, 11, 20), _utc(2026, 8, 17, 15, 30), DAY_HOP
    )
    assert start is not None and end is not None
    assert start.utcoffset() == timedelta(hours=-5)
    assert end.utcoffset() == timedelta(hours=-7)
    assert end.isoformat() == "2026-08-17T08:30:00-07:00"


def test_day_header_above_the_clock_is_skipped(tripit_local_time):
    """TripIt prints a date divider whenever the rendered day changes; the
    scan reaches past it to the clock line rather than giving up."""
    start, _ = tripit_local_time.local_times(
        _utc(2026, 8, 17, 11, 20), _utc(2026, 8, 17, 15, 30), DAY_HOP
    )
    assert start is not None
    assert start.isoformat() == "2026-08-17T06:20:00-05:00"


def test_single_location_record_stamps_only_its_start(tripit_local_time):
    """Lodging prints one clock, and its `Arrive` sits inside the marker line
    rather than on a line of its own. The end is TripIt's synthetic one-hour
    pad, rendered nowhere, so nothing authoritative stands behind it —
    borrowing the start's offset would assert one that could have changed in
    between (a DST transition inside the span)."""
    start, end = tripit_local_time.local_times(
        _utc(2026, 8, 17, 22, 0), _utc(2026, 8, 17, 23, 0), CHECK_IN
    )
    assert start is not None
    assert start.isoformat() == "2026-08-17T15:00:00-07:00"
    assert end is None


def test_check_in_line_is_not_read_as_a_clock(tripit_local_time):
    """`Check-In: 3:00pm` carries a time in a different shape on a line of
    its own. Matching it would resolve the wrong offset for records whose
    real clock line is missing."""
    start, _ = tripit_local_time.local_times(
        _utc(2026, 8, 17, 22, 0),
        _utc(2026, 8, 17, 23, 0),
        "\\n \\n\\n[Lodging] Arrive Somewhere\\nCheck-In: 3:00pm\\n",
    )
    assert start is None


@pytest.mark.parametrize(
    "description",
    [
        "no marker line at all\\nand no clock\\n",
        "\\n \\n\\n[Flight] SFO to BNA\\n",
        "\\n11:05 PM PDT\\nSouthwest Airlines\\n[Flight] SFO to BNA\\n",
        "",
    ],
    ids=["no-entry-marker", "no-clock-above", "content-between", "empty"],
)
def test_unreadable_shapes_fail_closed(tripit_local_time, description):
    """Every shape the parser does not recognise yields None so the caller
    stays on the UTC dates it already used. A guessed local date would move
    a night; a missing one changes nothing."""
    start, end = tripit_local_time.local_times(
        _utc(2026, 8, 23, 6, 5), _utc(2026, 8, 23, 10, 30), description
    )
    assert start is None
    assert end is None


def test_unresolvable_arrival_drops_only_the_end(tripit_local_time):
    """A departure that resolves and an arrival that does not keeps the half
    it knows — the start stamp is still an improvement over the UTC date."""
    description = "\\n6:20 AM CDT\\n[Flight] BNA to LAX\\n \\n\\nDelta 891\\nArrive Los Angeles\\n"
    start, end = tripit_local_time.local_times(
        _utc(2026, 8, 17, 11, 20), _utc(2026, 8, 17, 15, 30), description
    )
    assert start is not None
    assert start.isoformat() == "2026-08-17T06:20:00-05:00"
    assert end is None
