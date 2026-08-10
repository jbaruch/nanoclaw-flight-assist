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

# "DL2957 ATL to YYZ" — the carrier and number may or may not be spaced.
SUMMARY_RE = re.compile(r"^\s*([A-Z]{2})\s?(\d{1,4})\s+([A-Z]{3})\s+to\s+([A-Z]{3})\s*$")

# A departure inside this window is too close to act on: seats bought now
# rarely beat what check-in already assigned.
MIN_LEAD_HOURS = 12


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Upcoming flights for a seat check.")
    p.add_argument("--schedule", default=SCHEDULE_PATH)
    p.add_argument(
        "--now",
        required=True,
        help="Reference instant, ISO-8601 UTC. Injected so the output is deterministic.",
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
        print(f"upcoming-flights: {path} is not valid JSON — {exc}", file=sys.stderr)
        return 1
    except UnicodeDecodeError as exc:
        print(json.dumps({"error": "unreadable_schedule", "detail": str(exc)}))
        print(f"upcoming-flights: {path} is not UTF-8 text — {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        # PermissionError, ENOENT on a racing delete, a mount that vanished.
        print(json.dumps({"error": "unreadable_schedule", "detail": str(exc)}))
        print(f"upcoming-flights: cannot read {path} — {exc}", file=sys.stderr)
        return 1

    if isinstance(events, dict):
        events = events.get("events") or events.get("items") or []
    if not isinstance(events, list):
        # A valid JSON scalar parses fine and then explodes on iteration.
        print(json.dumps({"error": "bad_schedule", "detail": "root is not a list of events"}))
        print(
            f"upcoming-flights: {path} parsed but is a "
            f"{type(events).__name__}, not a list of events",
            file=sys.stderr,
        )
        return 1

    try:
        now = _parse_instant(args.now)
    except ScheduleError as exc:
        print(json.dumps({"error": "bad_now", "detail": str(exc)}))
        print(f"upcoming-flights: {exc}", file=sys.stderr)
        return 1

    flights = upcoming_flights(events, now, args.min_lead_hours)
    print(json.dumps({"flights": flights, "count": len(flights)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
