#!/usr/bin/env python3
"""Report fare-class inventory for one flight — the upgrade-certificate check.

Z on a SkyTeam partner is the motivating case: a Delta global upgrade needs Z
space on the OPERATING carrier's flight.

Reads the page's structured `bookingClassAvailability`, not the rendered grid.
Scraping the text pairs aircraft codes with the AM/PM of departure times and
invents flights that do not exist.

Output: one JSON object on stdout. Exit non-zero on failure.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import expertflyer_parse as ep  # noqa: E402
from expertflyer_session import (  # noqa: E402
    ExpertFlyerError,
    emit,
    fail,
    first_payload_with,
    goto_collecting,
    with_session,
)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Check fare-class inventory on a flight.")
    p.add_argument("--origin", required=True)
    p.add_argument("--destination", required=True)
    p.add_argument("--date", required=True, help="Departure date, YYYY-MM-DD")
    p.add_argument("--airline", required=True, help="Operating carrier, e.g. KL")
    p.add_argument("--flight", help="Flight number; omit to report every flight on the route")
    p.add_argument("--class", dest="fare_class", required=True, help="Fare class, e.g. Z")
    p.add_argument(
        "--include-codeshares",
        action="store_true",
        help="Inventory lives on the operating carrier; codeshares are excluded by default",
    )
    return p.parse_args(argv)


def summarise(flight: dict, fare_class: str) -> dict:
    bucket = flight["classes"].get(fare_class)
    return {
        "flight": f"{flight['marketing_carrier']}{flight['flight_number']}",
        "operating_carrier": flight["operating_carrier"],
        "is_codeshare": flight["is_codeshare"],
        "equipment": flight["equipment"],
        "departure": flight["departure"],
        "seats": bucket["seats"] if bucket else None,
        "available": bool(bucket and bucket["available"]),
        "display_capped": bool(bucket and bucket["display_capped"]),
    }


async def run(args) -> int:
    fare_class = args.fare_class.strip().upper()
    if len(fare_class) != 1 or not fare_class.isalpha():
        raise ValueError(f"--class expects a single fare-class letter, got {args.fare_class!r}")

    url = ep.availability_url(
        args.origin,
        args.destination,
        args.date,
        args.airline,
        fare_class,
        exclude_codeshares=not args.include_codeshares,
    )

    async def work(page):
        bodies = await goto_collecting(page, url)
        return first_payload_with(bodies, ep.availability_flights)

    flights = await with_session(work)
    if not flights:
        raise ExpertFlyerError(
            f"no structured availability for {args.airline.upper()} "
            f"{args.origin.upper()}-{args.destination.upper()} on {args.date} — "
            "the route may have no flights that day"
        )

    summaries = [summarise(f, fare_class) for f in flights]
    target = None
    if args.flight:
        match = ep.find_flight(flights, args.flight)
        target = summarise(match, fare_class) if match else None

    # A connecting itinerary repeats its first leg once per option, so the same
    # flight shows up several times in one result set.
    others, seen = [], set()
    for s in summaries:
        if target and s["flight"] == target["flight"]:
            continue
        key = (s["flight"], s["departure"])
        if key in seen:
            continue
        seen.add(key)
        others.append(s)
    payload = {
        "route": f"{args.origin.upper()}-{args.destination.upper()}",
        "date": args.date,
        "class": fare_class,
        "flight": target["flight"] if target else None,
        "seats": target["seats"] if target else None,
        "available": bool(target and target["available"]),
        "display_capped": bool(target and target["display_capped"]),
        "alternatives": [s for s in others if s["available"]],
        "recommend_alert": bool(target and not target["available"]),
    }
    if args.flight and target is None:
        payload["error_detail"] = (
            f"{args.airline.upper()}{args.flight} not found on this route/date; "
            "every parsed flight is in `alternatives`"
        )
        payload["alternatives"] = summaries
    return emit(payload)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(run(args))
    except (ExpertFlyerError, ValueError) as exc:
        return fail(exc)


if __name__ == "__main__":
    sys.exit(main())
