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
    goto_collecting,
    with_session,
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
    """Read the account's alert objects out of the page payload.

    The rendered /alerts page defaults to the Flight Alerts tab and says
    "No alerts found" even when seat alerts exist, so its text is not a usable
    source of truth.

    Aggregates every response rather than taking the first that parses: the
    alert list arrives split across payloads, and stopping at the first one
    missed the fare-class alerts entirely, so the duplicate guard let a second
    copy through.
    """
    bodies = await goto_collecting(page, ALERTS_URL, settle_ms=3500)
    found: list[dict] = []
    for body in bodies:
        found.extend(ep.extract_alerts(body))
    return list({a.get("id"): a for a in found}.values())


SEAT_ALERT_TYPE = "SEAT_MAP"


def alert_matches(alert: dict, args, class_code: str | None) -> bool:
    """Does this stored alert already watch what we are about to create?

    A seat alert and a fare-class alert on the same flight are different
    watches, so the alert type is part of the identity — otherwise creating a
    Z alert would be refused because a seat alert exists.
    """
    is_seat = alert.get("alertType") == SEAT_ALERT_TYPE
    if is_seat != (args.kind == "seat"):
        return False
    return (
        str(alert.get("flightNumber")) == str(args.flight)
        and (alert.get("airlineCode") or "").upper() == args.airline.upper()
        and (alert.get("departAirportCode") or "").upper() == args.origin.upper()
        and alert.get("status") == ACTIVE
        and (class_code is None or (alert.get("classCode") or "").upper() == class_code.upper())
    )


async def create_fare_class_alert(page, args, fare_class: str) -> str:
    """Open the per-row Create Alert modal for one flight and submit it.

    Row order is not assumed: each row's modal titles itself
    "Create Flight Alert (<flight number>)", so the right row is confirmed
    before anything is filled.
    """
    url = ep.availability_url(args.origin, args.destination, args.date, args.airline, fare_class)
    await goto_checked(page, url, settle_ms=4000)

    buttons = page.locator('button[title="Create Alert"]')
    count = await buttons.count()
    if not count:
        raise ExpertFlyerError(
            f"no Create Alert control on the {args.origin.upper()}-"
            f"{args.destination.upper()} {args.date} results — no flights that day"
        )

    wanted = str(args.flight).lstrip("0")
    for index in range(count):
        await buttons.nth(index).click()
        await page.wait_for_timeout(2000)
        # Read the modal container, not a text locator: "Create Flight Alert"
        # also matches an element that omits the flight number.
        modal = page.locator("div.fixed.inset-0").last
        title = (await modal.inner_text()) if await modal.count() else ""
        if f"({wanted})" in title.replace(" ", ""):
            break
        cancel = page.get_by_role("button", name="Cancel").first
        if await cancel.count():
            await cancel.click()
        else:
            await page.keyboard.press("Escape")
        await page.wait_for_timeout(800)
    else:
        raise ExpertFlyerError(
            f"{args.airline.upper()}{args.flight} has no row on this route/date — "
            "check the flight number with check-fare-class.py"
        )

    # Scope every field to the modal: ids repeat across the page, and the
    # modal's own copies are the only ones bound to this flight.
    modal = page.locator("div.fixed.inset-0").last
    name = await modal.locator("#alertName").first.input_value()

    # Class Code is a plain text input, NOT an autocomplete — no option list
    # appears. Never press Enter here: it submits the modal, which creates the
    # alert behind the verification step's back.
    class_box = modal.locator("#classType").first
    await class_box.click()
    await class_box.fill(fare_class)
    await page.wait_for_timeout(700)
    registered = (await class_box.input_value()).strip().upper()
    if not registered.startswith(fare_class.upper()):
        raise ExpertFlyerError(
            f"class code {fare_class} did not register on the form (got {registered!r})"
        )

    submit = modal.get_by_role("button", name="Create Alert", exact=False).last
    if await submit.is_disabled():
        raise ExpertFlyerError("Create Alert stayed disabled after filling the class code")
    await submit.click()
    await page.wait_for_timeout(4500)
    return name


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
    if args.kind == "seat":
        class_code = ep.cabin_code(args.cabin) if args.cabin else None
        if not class_code:
            raise ValueError("--cabin is required for a seat alert")
        values = ep.criterion_values(args.want)
    else:
        if not args.fare_class:
            raise ValueError("--class is required for a fare-class alert")
        class_code = args.fare_class.strip().upper()
        if len(class_code) != 1 or not class_code.isalpha():
            raise ValueError(f"--class expects one fare-class letter, got {args.fare_class!r}")
        values = ()

    async def work(page):
        before = await existing_alerts(page)
        duplicate = next((a for a in before if alert_matches(a, args, class_code)), None)
        if duplicate and not args.force:
            return emit(
                {
                    "created": False,
                    "reason": "already_exists",
                    "alert_id": duplicate.get("id"),
                    "alert_name": duplicate.get("name"),
                    "detail": "an active alert already watches this flight and class",
                }
            )

        if args.kind == "seat":
            name = await create_seat_alert(page, args, class_code, values)
        else:
            name = await create_fare_class_alert(page, args, class_code)
        after = await existing_alerts(page)
        return name, after

    outcome = await with_session(work)
    if isinstance(outcome, int):
        return outcome  # the duplicate guard already emitted
    name, after = outcome

    created = next((a for a in after if alert_matches(a, args, class_code)), None)
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
