#!/usr/bin/env python3
"""Create an ExpertFlyer alert, but only when the wanted thing is absent.

Refuses a redundant alert by default: an alert for something already bookable
delays the booking while the operator waits for an email describing space they
could have taken immediately. --force overrides.

Criteria bind by the checkbox `value` attribute (AISLE / WINDOW / ANY / EXIT /
TWO_TOGETHER). Index ordering and label text both fail: ids repeat across
hidden tab panels and the label text sits in a nested div. The <label for=...>
is clicked rather than the input, because React ignores a programmatic check.

Verification re-reads the account's alert objects from the page payload —
the /alerts page defaults to the Flight Alerts tab, which reads
"No alerts found" even when seat alerts exist.

WRITES to the live account. Output: one JSON object on stdout.
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
    goto_checked,
    session_page,
)

ALERTS_URL = "https://www.expertflyer.com/alerts"
ACTIVE = "ACTIVE"


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Create an ExpertFlyer alert.")
    p.add_argument("--kind", required=True, choices=("seat", "fare-class"))
    p.add_argument("--airline", required=True)
    p.add_argument("--flight", required=True)
    p.add_argument("--date", required=True, help="Departure date, YYYY-MM-DD")
    p.add_argument("--origin", required=True)
    p.add_argument("--destination", required=True)
    p.add_argument("--cabin", help="Seat alerts: cabin name or code")
    p.add_argument("--want", default="non-middle", help="Seat alerts: aisle,window | non-middle")
    p.add_argument("--class", dest="fare_class", help="Fare-class alerts: e.g. Z")
    p.add_argument("--force", action="store_true", help="Create even if already available")
    return p.parse_args(argv)


async def existing_alerts(page) -> list[dict]:
    """Read the account's alert objects out of the page payload."""
    payloads: list[str] = []

    async def capture(response):
        ctype = (await response.header_value("content-type")) or ""
        if "x-component" not in ctype and "json" not in ctype:
            return
        try:
            payloads.append(await response.text())
        except Exception:  # noqa: BLE001 - a body we cannot re-read is simply not a source
            pass

    page.on("response", capture)
    await goto_checked(page, ALERTS_URL, settle_ms=3500)
    page.remove_listener("response", capture)

    alerts: list[dict] = []
    for body in payloads:
        alerts.extend(ep.extract_alerts(body))
    return list({a.get("id"): a for a in alerts}.values())


def alert_matches(alert: dict, args, cabin: str | None) -> bool:
    return (
        str(alert.get("flightNumber")) == str(args.flight)
        and (alert.get("airlineCode") or "").upper() == args.airline.upper()
        and (alert.get("departAirportCode") or "").upper() == args.origin.upper()
        and alert.get("status") == ACTIVE
        and (cabin is None or (alert.get("classCode") or "").upper() == cabin.upper())
    )


async def create_seat_alert(page, args, cabin: str | None, values) -> str:
    if not cabin:
        raise ValueError("--cabin is required for a seat alert")
    url = ep.seat_map_url(
        args.origin, args.destination, args.date, args.airline, args.flight, cabin
    )
    await goto_checked(page, url, settle_ms=3500)

    button = page.get_by_role("button", name="Seat Alert").first
    if not await button.count():
        raise ExpertFlyerError(
            f"no Seat Alert control on the {cabin} seat map for "
            f"{args.airline.upper()}{args.flight} — the cabin may not exist on this aircraft"
        )
    await button.click()
    await page.wait_for_timeout(2500)

    name = await page.locator("#alertName:visible").first.input_value()
    for value in values:
        box = page.locator(f'input[type=checkbox][value="{value}"]:visible').first
        if not await box.count():
            raise ExpertFlyerError(f"no criterion checkbox with value={value}")
        if await box.is_disabled():
            raise ExpertFlyerError(f"criterion {value} is disabled for this search")
        box_id = await box.get_attribute("id")
        await page.locator(f'label[for="{box_id}"]:visible').first.click()
        await page.wait_for_timeout(400)
        if not await box.is_checked():
            raise ExpertFlyerError(f"criterion {value} did not register")

    submit = page.get_by_role("button", name="Create Alert").first
    if await submit.is_disabled():
        raise ExpertFlyerError("Create Alert stayed disabled after ticking criteria")
    await submit.click()
    await page.wait_for_timeout(4000)
    return name


async def run(args) -> int:
    cabin = ep.cabin_code(args.cabin) if args.cabin else None
    if args.kind == "seat":
        if not cabin:
            raise ValueError("--cabin is required for a seat alert")
        values = ep.criterion_values(args.want)
    else:
        if not args.fare_class:
            raise ValueError("--class is required for a fare-class alert")
        values = ()

    async with session_page() as page:
        before = await existing_alerts(page)
        duplicate = next((a for a in before if alert_matches(a, args, cabin)), None)
        if duplicate and not args.force:
            return emit(
                {
                    "created": False,
                    "reason": "already_exists",
                    "alert_id": duplicate.get("id"),
                    "alert_name": duplicate.get("name"),
                    "detail": "an active alert already watches this flight and cabin",
                }
            )

        if args.kind != "seat":
            raise ExpertFlyerError(
                "fare-class alert creation is not implemented — only seat alerts are "
                "wired end to end. Use check-fare-class.py to report inventory."
            )

        name = await create_seat_alert(page, args, cabin, values)
        after = await existing_alerts(page)

    created = next((a for a in after if alert_matches(a, args, cabin)), None)
    if created is None:
        raise ExpertFlyerError(
            f"submitted the alert for {args.airline.upper()}{args.flight} but it does "
            "not appear in the account's alerts — treat as NOT created"
        )
    return emit(
        {
            "created": True,
            "alert_id": created.get("id"),
            "alert_name": created.get("name") or name,
            "flight": f"{created.get('airlineCode')}{created.get('flightNumber')}",
            "route": f"{created.get('departAirportCode')}-{created.get('arriveAirportCode')}",
            "cabin": created.get("cabinName") or created.get("classCode"),
            "criteria": created.get("seatMapLocations") or list(values),
            "status": created.get("status"),
            "verified_in_account": True,
        }
    )


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(run(args))
    except (ExpertFlyerError, ValueError) as exc:
        return fail(exc)


if __name__ == "__main__":
    sys.exit(main())
