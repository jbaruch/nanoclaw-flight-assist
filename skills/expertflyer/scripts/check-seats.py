#!/usr/bin/env python3
"""Report bookable seats matching the operator's criteria on one flight.

Reads the seat map's structured payload — the page ships each seat's `status`
and its own isWindow/isAisle/isMiddle flags — so nothing is inferred from the
rendered legend.

Route is optional: given only a flight number, it is resolved via the flight
status page first, because the seat-map URL requires a city pair.

Output: one JSON object on stdout. Exit non-zero on failure.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import expertflyer_parse as ep  # noqa: E402
from expertflyer_session import (  # noqa: E402
    ExpertFlyerError,
    emit,
    fail,
    first_payload_with,
    goto_checked,
    goto_collecting,
    with_session,
)

# On the status results page each airport code sits on its own line, with
# "Term: S" / "Gate: D16" lines between them — so a proximity regex across the
# flattened text does not work. Match whole lines instead.
AIRPORT_LINE_RE = re.compile(r"^[A-Z]{3}$")
RESULTS_MARKER = "Flight Status Results"


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Check seat availability on a flight.")
    p.add_argument("--airline", required=True, help="Marketing/operating carrier, e.g. DL")
    p.add_argument("--flight", required=True, help="Flight number, e.g. 2957")
    p.add_argument("--date", required=True, help="Departure date, YYYY-MM-DD")
    p.add_argument("--cabin", required=True, help="Cabin name or code, e.g. 'comfort+' / W")
    p.add_argument("--want", default="non-middle", help="aisle,window | non-middle | any | middle")
    p.add_argument("--origin", help="Origin IATA (resolved from the flight if omitted)")
    p.add_argument("--destination", help="Destination IATA (resolved if omitted)")
    return p.parse_args(argv)


async def resolve_route(page, airline: str, flight: str, date: str) -> tuple[str, str]:
    """Resolve a flight number to its city pair via the status page."""
    await goto_checked(page, ep.status_url(airline, flight, date))
    body = await page.inner_text("body")
    # Slice off the nav, whose "FAQ" / "SWU" entries are also bare 3-letter lines.
    marker = body.find(RESULTS_MARKER)
    window = body[marker:] if marker >= 0 else body
    codes = [ln.strip() for ln in window.splitlines() if AIRPORT_LINE_RE.match(ln.strip())]
    if len(codes) < 2:
        raise ExpertFlyerError(
            f"could not resolve a route for {airline}{flight} on {date} "
            f"(found {codes}) — pass --origin/--destination explicitly"
        )
    return codes[0], codes[1]


async def run(args) -> int:
    cabin = ep.cabin_code(args.cabin)
    wants = ep.normalize_wants(args.want)

    async def work(page):
        origin, destination = args.origin, args.destination
        if not (origin and destination):
            origin, destination = await resolve_route(page, args.airline, args.flight, args.date)

        url = ep.seat_map_url(origin, destination, args.date, args.airline, args.flight, cabin)
        bodies = await goto_collecting(page, url)
        seat_map = first_payload_with(bodies, lambda body: ep.extract_json_object(body, "seatMap"))
        if seat_map is None:
            raise ExpertFlyerError(
                f"no seat map in the {len(bodies)} responses for {args.airline}"
                f"{args.flight} {origin}-{destination} {args.date} cabin {cabin} — "
                "the cabin may not exist on this aircraft"
            )

        total_seats = sum(1 for _ in ep.iter_seats(seat_map))
        matching = ep.matching_seats(seat_map, wants)
        # A cabin the aircraft does not have yields an empty map, which reads
        # identically to a full one. Alerting on it would watch a cabin that
        # can never open.
        cabin_present = total_seats > 0
        return emit(
            {
                "flight": f"{args.airline.upper()}{args.flight}",
                "route": f"{origin}-{destination}",
                "date": args.date,
                "cabin": cabin,
                "cabin_present": cabin_present,
                "wanted": list(wants),
                "matching": matching,
                "seats_in_cabin": total_seats,
                "available_total": len(ep.available_seats(seat_map)),
                "recommend_alert": cabin_present and ep.recommend_alert(matching),
            }
        )

    return await with_session(work)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(run(args))
    except (ExpertFlyerError, ValueError) as exc:
        return fail(exc)


if __name__ == "__main__":
    sys.exit(main())
