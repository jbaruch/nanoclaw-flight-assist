#!/usr/bin/env python3
"""List upcoming flights worth a seat check, from the travel schedule.

The seat pass needs a work list: which flights exist, and how to name them to
the ExpertFlyer service. That extraction is deterministic — parse the schedule,
drop anything that is not an upcoming flight, and shape what remains — so it
belongs in a script rather than in agent judgement.

This performs no network call. The skill runs it to get the list, then runs
`expertflyer.py seats` per flight.

The reference time is injected with `--now`, never read from the clock, so a
test that passes today passes every day.

Output: one JSON object on stdout. Exit non-zero on failure.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEDULE_PATH = "/workspace/group/travel-schedule.json"
FLIGHT_TYPE = "Flight"
TRIP_TYPE = "Trip"

# "DL2957 ATL to YYZ" — the carrier and number may or may not be spaced.
SUMMARY_RE = re.compile(r"^\s*([A-Z]{2})\s?(\d{1,4})\s+([A-Z]{3})\s+to\s+([A-Z]{3})\s*$")

# A departure inside this window is too close to act on: seats bought now
# rarely beat what check-in already assigned.
MIN_LEAD_HOURS = 12

# How many upcoming trips the pass covers. Every flight costs the caller a
# request per cabin against a bot-walled service, and the whole upcoming
# schedule is months of them — the question is about the next trip, not the
# year. Widen deliberately with --trips.
DEFAULT_TRIPS = 1

# Trip windows are date-only while a departure is a UTC instant, so a flight
# leaving late in the local evening lands on the next UTC day and falls a day
# outside its own trip. A day of slack each side absorbs that without reaching
# the next trip, which is separated by far more.
TRIP_EDGE_SLACK = timedelta(days=1)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Upcoming flights for a seat check.")
    p.add_argument("--schedule", default=SCHEDULE_PATH)
    p.add_argument(
        "--now",
        required=True,
        help="Reference instant, ISO-8601 UTC. Injected so the output is deterministic.",
    )
    p.add_argument(
        "--trips",
        type=int,
        default=DEFAULT_TRIPS,
        help=(
            "How many upcoming trips to cover. 0 covers every upcoming flight, "
            "which is a request per cabin per flight against a bot-walled service."
        ),
    )
    p.add_argument(
        "--min-lead-hours",
        type=int,
        default=MIN_LEAD_HOURS,
        help="Skip departures sooner than this many hours away.",
    )
    return p.parse_args(argv)


def _parse_instant(value: str) -> datetime:
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ScheduleError(
            f"{value!r} is not an ISO-8601 instant — expected e.g. 2026-08-09T00:00:00Z"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class ScheduleError(ValueError):
    """A malformed schedule or reference instant, reported not raised."""


def flight_from_event(event: dict) -> dict | None:
    """Shape one schedule event into a flight, or None when it is not one."""
    if event.get("type") != FLIGHT_TYPE:
        return None
    match = SUMMARY_RE.match(str(event.get("summary", "")))
    if not match:
        return None
    airline, number, origin, destination = match.groups()
    start = event.get("start")
    if not start:
        return None
    try:
        departs = _parse_instant(str(start))
    except ScheduleError:
        # One unparseable timestamp should not lose the whole schedule; the
        # event is skipped exactly as an unparseable summary is.
        return None
    return {
        "airline": airline,
        "flight": number,
        "origin": origin,
        "destination": destination,
        # The schedule stamps UTC. A departure late in the local evening can
        # fall on the next UTC day, so the service's own status lookup is the
        # authority on the operating date — see the skill's fallback note.
        "date": departs.date().isoformat(),
        "departs_utc": departs.isoformat().replace("+00:00", "Z"),
        "summary": event.get("summary"),
        "uid": event.get("uid"),
    }


def trip_from_event(event: dict) -> dict | None:
    """Shape one schedule event into a trip window, or None when it is not one."""
    if event.get("type") != TRIP_TYPE:
        return None
    start, end = event.get("start"), event.get("end")
    if not start or not end:
        return None
    try:
        opens = _parse_instant(str(start))
        closes = _parse_instant(str(end))
    except ScheduleError:
        return None
    return {
        "summary": event.get("summary"),
        "uid": event.get("uid"),
        "start": opens.date().isoformat(),
        "end": closes.date().isoformat(),
        "opens": opens - TRIP_EDGE_SLACK,
        # The end DATE is inclusive, so the window runs to the end of that day
        # before the slack is added on top.
        "closes": closes + timedelta(days=1) + TRIP_EDGE_SLACK,
    }


def upcoming_trips(events, now: datetime, limit: int) -> list[dict]:
    """The next `limit` trips that have not ended, soonest first.

    A trip already over cannot hold a flight worth a seat check, and a limit of
    zero means every one of them.
    """
    found = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        trip = trip_from_event(event)
        if trip is None or trip["closes"] < now:
            continue
        found[str(trip["uid"] or f"{trip['summary']}{trip['start']}")] = trip
    ordered = sorted(found.values(), key=lambda t: (t["opens"], t["start"]))
    return ordered if limit == 0 else ordered[:limit]


def _within(trip: dict, departs: datetime) -> bool:
    return trip["opens"] <= departs <= trip["closes"]


def _in_any_trip(trips, flight: dict) -> bool:
    departs = _parse_instant(flight["departs_utc"])
    return any(_within(trip, departs) for trip in trips)


def upcoming_flights(events, now: datetime, min_lead_hours: int) -> list[dict]:
    """Flights departing far enough ahead to be worth acting on, soonest first."""
    cutoff = now + timedelta(hours=min_lead_hours)
    found: dict[str, dict] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        flight = flight_from_event(event)
        if flight is None:
            continue
        if _parse_instant(flight["departs_utc"]) < cutoff:
            continue
        # The same segment can appear twice across a re-synced schedule; the
        # uid is stable, so last write wins rather than double-reporting.
        # Without one, the key must carry route and departure too: a through
        # flight keeps its number across legs on the same date, so keying on
        # carrier+number+date alone would silently drop one of them.
        fallback = (
            f"{flight['airline']}{flight['flight']}"
            f"{flight['origin']}{flight['destination']}{flight['departs_utc']}"
        )
        found[str(flight["uid"] or fallback)] = flight
    return sorted(found.values(), key=lambda f: f["departs_utc"])


def main(argv=None) -> int:
    args = parse_args(argv)
    path = Path(args.schedule)
    if not path.is_file():
        print(json.dumps({"error": "no_schedule", "detail": f"{path} not found"}))
        print(
            f"upcoming-flights: {path} not found — the nightly sync writes it; "
            "run tessl__nightly-travel-sync first",
            file=sys.stderr,
        )
        return 1
    try:
        events = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        print(json.dumps({"error": "bad_schedule", "detail": str(exc)}))
        print(
            f"upcoming-flights: {path} is not valid JSON ({exc}) — "
            "regenerate it with tessl__nightly-travel-sync, or restore the file from a backup",
            file=sys.stderr,
        )
        return 1
    except UnicodeDecodeError as exc:
        print(json.dumps({"error": "unreadable_schedule", "detail": str(exc)}))
        print(
            f"upcoming-flights: {path} is not UTF-8 text ({exc}) — "
            "regenerate it with tessl__nightly-travel-sync so it is rewritten as UTF-8",
            file=sys.stderr,
        )
        return 1
    except OSError as exc:
        # PermissionError, ENOENT on a racing delete, a mount that vanished.
        print(json.dumps({"error": "unreadable_schedule", "detail": str(exc)}))
        print(
            f"upcoming-flights: cannot read {path} ({exc}) — check the group "
            "volume is mounted and the file is readable by the agent user, then "
            "regenerate it with tessl__nightly-travel-sync",
            file=sys.stderr,
        )
        return 1

    if isinstance(events, dict):
        events = events.get("events") or events.get("items") or []
    if not isinstance(events, list):
        # A valid JSON scalar parses fine and then explodes on iteration.
        print(json.dumps({"error": "bad_schedule", "detail": "root is not a list of events"}))
        print(
            f"upcoming-flights: {path} parsed but is a "
            f"{type(events).__name__}, not a list of events — the schedule must "
            "be a JSON list, or an object with an events/items key; "
            "regenerate it with tessl__nightly-travel-sync",
            file=sys.stderr,
        )
        return 1

    try:
        now = _parse_instant(args.now)
    except ScheduleError as exc:
        print(json.dumps({"error": "bad_now", "detail": str(exc)}))
        print(f"upcoming-flights: {exc}", file=sys.stderr)
        return 1

    if args.trips < 0:
        print(json.dumps({"error": "bad_trips", "detail": f"--trips {args.trips} is negative"}))
        print(
            f"upcoming-flights: --trips {args.trips} is negative — pass a count of "
            "trips to cover, or 0 for every upcoming flight",
            file=sys.stderr,
        )
        return 1

    flights = upcoming_flights(events, now, args.min_lead_hours)
    trips = upcoming_trips(events, now, args.trips)
    if args.trips == 0:
        covered, excluded = flights, []
    else:
        covered = [f for f in flights if _in_any_trip(trips, f)]
        excluded = [f for f in flights if f not in covered]

    # A dropped flight is reported, never silently absent: a caller that reads
    # `flights` as "everything upcoming" would tell the operator their seats
    # are fine on a trip it never looked at.
    print(
        json.dumps(
            {
                "flights": covered,
                "count": len(covered),
                "trips": [
                    {k: v for k, v in trip.items() if k not in ("opens", "closes")}
                    for trip in trips
                ],
                "excluded_count": len(excluded),
                "excluded": excluded,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
