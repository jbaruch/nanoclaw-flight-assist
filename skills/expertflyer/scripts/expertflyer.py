#!/usr/bin/env python3
"""Thin client for the ExpertFlyer API service.

The browser automation, the ExpertFlyer credential and the minted session all
live in `jbaruch/expertflyer-api`, a service container that runs no LLM. This
container holds none of them — it makes HTTP calls and relays the answers.

Stdlib only: the work here is a request and an error mapping, which does not
earn a dependency.

Output: one JSON object on stdout. Exit non-zero on failure, with the service's
own diagnostic on stderr.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import seat_quality  # noqa: E402

URL_ENV = "EXPERTFLYER_API_URL"
TOKEN_ENV = "EXPERTFLYER_API_TOKEN"
# The service runs beside the agent container on the same host. Address the
# docker bridge gateway by IP, NOT host.docker.internal: that alias sits in
# nanoclaw's AGENT_PROXY_BYPASS_HOSTS, so a request to the hostname skips the
# OneCLI gateway — and the gateway is what swaps the real bearer in for the
# `onecli-managed` placeholder this container holds. Using the hostname would
# send the placeholder through unswapped and earn a 401.
DEFAULT_URL = "http://172.17.0.1:8090"
TIMEOUT_SECONDS = 180


def _base_url() -> str:
    return os.environ.get(URL_ENV, DEFAULT_URL).rstrip("/")


def _request(method: str, path: str, params: dict | None = None, body: dict | None = None):
    url = f"{_base_url()}{path}"
    if params:
        cleaned = {k: v for k, v in params.items() if v is not None}
        url = f"{url}?{urllib.parse.urlencode(cleaned)}"

    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    token = os.environ.get(TOKEN_ENV)
    if token:
        request.add_header("Authorization", f"Bearer {token}")

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        # The service distinguishes an expired session (503) from the upstream
        # bot wall (502); both look like 403 to it, so the distinction is not
        # re-derivable here. Relay it rather than flattening it.
        raw = exc.read().decode(errors="replace")
        try:
            detail = json.loads(raw).get("detail", raw)
        except json.JSONDecodeError:
            detail = raw
        if isinstance(detail, dict):
            return {"error": detail.get("error", "upstream"), "detail": detail.get("detail", raw)}
        return {"error": "upstream", "detail": detail, "status": exc.code}
    except urllib.error.URLError as exc:
        # A TLS verification failure means the service ANSWERED and its
        # certificate could not be validated — telling the operator to check
        # whether it is running sends them to the wrong place entirely.
        if isinstance(exc.reason, ssl.SSLCertVerificationError):
            return {
                "error": "tls",
                "detail": (
                    f"TLS verification failed for {_base_url()} "
                    f"({exc.reason.verify_message or exc.reason.reason}) — the service "
                    "responded but its certificate chain could not be verified. On a "
                    "host whose Python does not read the system trust store (macOS), "
                    "point SSL_CERT_FILE at the system CA bundle: "
                    "SSL_CERT_FILE=/etc/ssl/cert.pem on macOS, "
                    "/etc/ssl/certs/ca-certificates.crt on Debian"
                ),
            }
        return {
            "error": "unreachable",
            "detail": (
                f"ExpertFlyer API not reachable at {_base_url()} ({exc.reason}) — "
                f"check the service is running and {URL_ENV} points at it"
            ),
        }


# The schedule stamps UTC, so a departure late in the local evening falls on
# the next UTC day and the service finds no such flight. Retrying the previous
# day is fixed logic, not judgement, so it lives here rather than in the skill.
ROUTE_UNRESOLVED = "could not resolve a route"


def _previous_day(date: str) -> str:
    return (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")


def _looks_like_wrong_date(result: dict) -> bool:
    detail = str(result.get("detail", "")).lower()
    return "error" in result and ROUTE_UNRESOLVED in detail


def _rank(result: dict) -> dict:
    """Order the service's bookable seats by the operator's preferences.

    The service reports what each seat IS; ranking decides what it is WORTH,
    which is why it happens here rather than upstream. `matching` is the
    service's own criteria filter and is left untouched.
    """
    if "error" in result or not isinstance(result.get("seats"), list):
        return result
    cabin = result.get("cabin")
    # Recline is derived from the cabin's exit-row layout, so descriptions need
    # the same tiers ranking used. Without them a derived reclining row renders
    # as a plain "exit row", because service seats carry no recline field.
    # `exit_rows` is the cabin's full layout when the service supplies it.
    # Without it the seats list holds bookable seats only, so an occupied rear
    # exit row is invisible and nothing is claimed to recline.
    layout = result.get("exit_rows")
    try:
        tiers = seat_quality.exit_tiers(result["seats"], layout)
        ranked = seat_quality.rank_seats(result["seats"], cabin, layout)
        result["ranked"] = [{**s, "why": seat_quality.describe(s, cabin, tiers)} for s in ranked]
        best = ranked[0] if ranked else None
        result["best"] = seat_quality.describe(best, cabin, tiers) if best else None
        # Every open seat may still be unacceptable — a cabin of middles ranks
        # to nothing even though `available_total` is non-zero.
        result["acceptable_total"] = len(ranked)
    except seat_quality.SeatQualityError as exc:
        # A seat the ranker refuses to order is a reportable answer, not a
        # traceback: the service replied, and the operator needs to read WHICH
        # seat could not be ranked. Dropping `best`/`ranked` is deliberate —
        # a partial ranking would read as a complete one.
        result.pop("ranked", None)
        result.pop("best", None)
        result.pop("acceptable_total", None)
        result["error"] = "unrankable"
        result["detail"] = str(exc)
    return result


# How far up the cabin ladder to look by default. Each rung is one more request
# to a bot-walled service, and one rung already covers the case a single-cabin
# check structurally cannot see: a Comfort+ seat opening while the operator
# sits in Main.
DEFAULT_SCAN_RUNGS = 1

VERDICT_OPTIMAL = "optimal"
VERDICT_UPGRADE = "upgrade"
VERDICT_NO_HELD_SEAT = "no_held_seat"
VERDICT_POSITION_UNKNOWN = "held_position_unknown"
VERDICT_EXIT_ROW_UNKNOWN = "held_exit_row_unknown"


def _upgrades_over(held: dict, open_seats, layout) -> list[dict]:
    """Open seats that beat the held one, best first, each described."""
    beats = [s for s in open_seats if seat_quality.is_upgrade(s, held, None, layout or None)]
    ranked = seat_quality.rank_seats(beats, None, layout or None)
    tiers = seat_quality.exit_tiers(beats, layout or None)
    return [{**s, "why": seat_quality.describe(s, None, tiers)} for s in ranked]


def _reported_labels(upgrades) -> str:
    return ", ".join(str(s.get("label")) for s in upgrades)


def _held_seat(label: str, cabin: str, position: str | None, cabin_seats, exit_rows) -> dict:
    """The seat already occupied, shaped so it can be ranked against open ones.

    The service reports bookable seats, so the held seat is absent from every
    response by definition — it is occupied, by the operator. Its row and
    column come from the designator; whether it is an exit row comes from the
    cabin's layout; its position is either stated or read off the columns of
    the open seats around it.
    """
    row, column = seat_quality.parse_seat_label(label)
    source = "stated"
    if position is None:
        position = seat_quality.column_positions(cabin_seats).get(column)
        source = "seat-map"
    return {
        "label": f"{row}{column}",
        "row": row,
        "column": column,
        # None when the column is stated nowhere the sweep can read. The caller
        # reports that rather than ranking a seat whose position it invented.
        "position": position,
        "position_source": source if position else None,
        "cabin": cabin,
        # None when the service sent no layout. An absent `exit_rows` and one
        # that excludes this row both render as False otherwise, and the two
        # mean opposite things: the second is evidence, the first is its
        # absence. Reading absence as False demotes an exit row the operator
        # holds, which reports a worse open seat as an upgrade.
        "isExitRow": None if exit_rows is None else int(row) in {int(r) for r in exit_rows},
        "exit_row_source": None if exit_rows is None else "layout",
    }


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Query the ExpertFlyer API service.")
    sub = p.add_subparsers(dest="action", required=True)

    seats = sub.add_parser("seats", help="Bookable seats matching the wanted positions")
    seats.add_argument("--airline", required=True)
    seats.add_argument("--flight", required=True)
    seats.add_argument("--date", required=True, help="YYYY-MM-DD")
    seats.add_argument("--cabin", required=True, help="e.g. 'premium economy', 'comfort+', W")
    seats.add_argument("--want", default="non-middle")
    seats.add_argument("--origin")
    seats.add_argument("--destination")
    seats.add_argument(
        "--date-fallback",
        action="store_true",
        help=(
            "Retry the previous day when the flight is not found on --date. "
            "Only for dates derived from the UTC travel schedule, where a late "
            "local departure lands on the next UTC day. Off by default: for a "
            "date the operator named, a flight that does not operate that day "
            "must report that, not seats from another day's flight."
        ),
    )

    assess = sub.add_parser("assess", help="Judge the held seat against everything open")
    assess.add_argument("--airline", required=True)
    assess.add_argument("--flight", required=True)
    assess.add_argument("--date", required=True, help="YYYY-MM-DD")
    assess.add_argument(
        "--held",
        help=(
            "The seat currently assigned, e.g. 21F. Without it there is no "
            "verdict: an open seat can only be better or worse than something."
        ),
    )
    assess.add_argument("--held-cabin", required=True, help="e.g. 'comfort+', 'first', W")
    assess.add_argument(
        "--held-position",
        choices=("window", "aisle", "middle"),
        help=(
            "Whether the held seat is a window, an aisle or a middle. Omit it "
            "and the column is read off the open seats in the same cabin; that "
            "fails when no open seat shares the column, which reports "
            "held_position_unknown rather than guessing."
        ),
    )
    assess.add_argument(
        "--scan-up",
        type=int,
        default=DEFAULT_SCAN_RUNGS,
        help=(
            "How many cabins above the held one to include. Each is one more "
            f"request to a bot-walled service. Default {DEFAULT_SCAN_RUNGS}; "
            "0 checks the held cabin alone."
        ),
    )
    assess.add_argument("--origin")
    assess.add_argument("--destination")
    assess.add_argument("--date-fallback", action="store_true")

    fare = sub.add_parser("fare-class", help="Fare-class inventory for a flight")
    fare.add_argument("--origin", required=True)
    fare.add_argument("--destination", required=True)
    fare.add_argument("--date", required=True, help="YYYY-MM-DD")
    fare.add_argument("--airline", required=True)
    fare.add_argument("--class", dest="fare_class", required=True)
    fare.add_argument("--flight")
    fare.add_argument("--include-codeshares", action="store_true")

    sub.add_parser("alerts", help="Every alert on the account")

    create = sub.add_parser("create-alert", help="Create a seat or fare-class alert")
    create.add_argument("--kind", required=True, choices=("seat", "fare-class"))
    create.add_argument("--airline", required=True)
    create.add_argument("--flight", required=True)
    create.add_argument("--date", required=True, help="YYYY-MM-DD")
    create.add_argument("--origin", required=True)
    create.add_argument("--destination", required=True)
    create.add_argument("--cabin")
    create.add_argument("--want", default="non-middle")
    create.add_argument("--class", dest="fare_class")
    create.add_argument("--force", action="store_true")

    delete = sub.add_parser("delete-alert", help="Delete one alert by id")
    delete.add_argument("--id", dest="alert_id", required=True, type=int)

    return p.parse_args(argv)


def _seats_in_cabin(args, cabin: str, want: str) -> dict:
    """One cabin's bookable seats, with the schedule-date retry applied.

    Shared by `seats` and `assess`: the previous-day retry is a property of a
    schedule-derived date, not of which command asked.
    """

    def seats_on(date: str) -> dict:
        return _request(
            "GET",
            "/seats",
            {
                "airline": args.airline,
                "flight": args.flight,
                "date": date,
                "cabin": cabin,
                "want": want,
                "origin": args.origin,
                "destination": args.destination,
            },
        )

    result = seats_on(args.date)
    # Opt-in only. The retry is sound when the date came from the UTC
    # schedule; against a date the operator named it would answer about a
    # different day's flight and present it as the requested one.
    if not (_looks_like_wrong_date(result) and args.date_fallback):
        return result
    try:
        fallback = _previous_day(args.date)
    except ValueError:
        return {
            "error": "bad_request",
            "detail": (
                f"--date {args.date!r} is not YYYY-MM-DD, so the "
                "previous-day retry cannot be computed — pass the "
                "departure date as e.g. 2026-08-31"
            ),
        }
    retried = seats_on(fallback)
    # Report the retry's own outcome. Returning the first error instead
    # would hide what actually went wrong the second time — an expired
    # session or an unreachable service reported as "no such flight".
    if "error" not in retried:
        retried["date_fallback_applied"] = fallback
    else:
        retried["date_fallback_attempted"] = fallback
    return retried


def _assess(args) -> dict:
    """Judge the seat already held against everything open worth moving to.

    This is the question the operator actually asks — "are my seats the best"
    — and it is not the one a cabin scan answers. A cabin scan reports what is
    open; only a comparison against the held seat reports whether any of it is
    better. Without the held seat there is no verdict to give, so the absence
    is reported rather than answered around.
    """
    if not args.held:
        return {
            "verdict": VERDICT_NO_HELD_SEAT,
            "detail": (
                "no seat given, so nothing can be called better or worse than it — "
                "pass --held with the seat currently assigned, e.g. --held 21F"
            ),
        }
    try:
        held_cabin = seat_quality.cabin_code(args.held_cabin)
        cabins = seat_quality.cabins_at_or_above(held_cabin, args.scan_up)
        row, column = seat_quality.parse_seat_label(args.held)
    except seat_quality.SeatQualityError as exc:
        return {"error": "bad_request", "detail": str(exc)}

    scanned: dict[str, dict] = {}
    absent: list[str] = []
    for cabin in cabins:
        # `want=any`: the criteria filter shapes `matching`, and a comparison
        # against the held seat has to see every open seat, not the subset that
        # already matched a wanted position.
        response = _seats_in_cabin(args, cabin, "any")
        if "error" in response:
            # A cabin that failed to load could be holding the upgrade, so a
            # partial sweep must not report "nothing better is open".
            return {
                "error": response["error"],
                "detail": f"{cabin}: {response.get('detail', response['error'])}",
                "cabin_failed": cabin,
                "cabins_requested": cabins,
            }
        if response.get("cabin_present") is False:
            absent.append(cabin)
            continue
        scanned[cabin] = response

    if held_cabin not in scanned:
        return {
            "error": "bad_request",
            "detail": (
                f"the aircraft has no {held_cabin} cabin, so seat {args.held!r} "
                "cannot be in it — check the cabin the operator actually flies"
            ),
            "cabins_absent": absent,
        }

    held = _held_seat(
        args.held,
        held_cabin,
        args.held_position,
        scanned[held_cabin].get("seats", []),
        scanned[held_cabin].get("exit_rows"),
    )
    common = {
        "flight": scanned[held_cabin].get("flight"),
        "route": scanned[held_cabin].get("route"),
        "cabins_scanned": sorted(scanned, key=lambda c: seat_quality.CABIN_SCORE[c], reverse=True),
        "cabins_absent": absent,
        # `optimal` is only ever true of the cabins actually read. Naming the
        # ones the sweep stopped short of keeps the verdict from being heard
        # as "nothing anywhere on this aircraft beats your seat".
        "cabins_unscanned": seat_quality.cabins_above(cabins[0]),
    }
    if held["position"] is None:
        return {
            **common,
            "verdict": VERDICT_POSITION_UNKNOWN,
            "held": held,
            "detail": (
                f"seat {args.held!r} is a {held_cabin} seat in column {column} of row {row}, "
                f"and no open seat in that cabin sits in column {column} — so whether it is a "
                "window, an aisle or a middle cannot be read off the seat map. Pass "
                "--held-position window|aisle|middle."
            ),
        }

    # Exit rows are numbered on the aircraft, not per cabin, so recline is
    # derived from every layout the sweep saw rather than one cabin's slice.
    layout = sorted({int(r) for c in scanned.values() for r in (c.get("exit_rows") or [])})
    open_seats = [seat for response in scanned.values() for seat in response.get("seats", [])]
    try:
        described = _upgrades_over(held, open_seats, layout)
        held["why"] = seat_quality.describe(
            held, None, seat_quality.exit_tiers([held], layout or None)
        )
        # The layout was missing, so whether the held seat sits in an exit row
        # is unknown. Settle it only when it changes the answer: rank against
        # both readings, and report the ambiguity when they disagree.
        if held["isExitRow"] is None:
            either_way = [
                _upgrades_over({**held, "isExitRow": claim}, open_seats, layout)
                for claim in (True, False)
            ]
            if _reported_labels(either_way[0]) != _reported_labels(either_way[1]):
                return {
                    **common,
                    "verdict": VERDICT_EXIT_ROW_UNKNOWN,
                    "held": held,
                    "detail": (
                        f"the service sent no exit-row layout for {held_cabin}, so whether "
                        f"seat {args.held!r} is in an exit row is unknown — and it decides the "
                        f"verdict here: {_reported_labels(either_way[1]) or 'nothing'} beats it "
                        f"if it is an ordinary row, {_reported_labels(either_way[0]) or 'nothing'} "
                        "if it is an exit row. Ask the operator whether the seat is in an exit "
                        "row, or re-run when the service reports the layout."
                    ),
                }
            described = either_way[1]
    except seat_quality.SeatQualityError as exc:
        return {**common, "error": "unrankable", "detail": str(exc), "held": held}

    return {
        **common,
        # `optimal` is scoped to `cabins_scanned`, never to the whole aircraft.
        "verdict": VERDICT_UPGRADE if described else VERDICT_OPTIMAL,
        "held": held,
        "upgrades": described,
        "best_upgrade": described[0]["why"] if described else None,
        # Nothing open beats the held seat, so watching is the only move left.
        # A seat worth taking is taken now, not watched.
        "alert_recommended": not described,
    }


def run(args) -> dict:
    if args.action == "seats":
        return _rank(_seats_in_cabin(args, args.cabin, args.want))
    if args.action == "assess":
        return _assess(args)
    if args.action == "fare-class":
        return _request(
            "GET",
            "/fare-class",
            {
                "origin": args.origin,
                "destination": args.destination,
                "date": args.date,
                "airline": args.airline,
                "class": args.fare_class,
                "flight": args.flight,
                "include_codeshares": str(args.include_codeshares).lower(),
            },
        )
    if args.action == "alerts":
        return _request("GET", "/alerts")
    if args.action == "create-alert":
        return _request(
            "POST",
            "/alerts",
            body={
                "kind": args.kind,
                "airline": args.airline,
                "flight": args.flight,
                "date": args.date,
                "origin": args.origin,
                "destination": args.destination,
                "cabin": args.cabin,
                "want": args.want,
                "class": args.fare_class,
                "force": args.force,
            },
        )
    if args.action == "delete-alert":
        return _request("DELETE", f"/alerts/{args.alert_id}")
    raise ValueError(f"unknown action {args.action!r}")


# Verdicts that decline to answer. They are not service faults, but treating
# them as success invites the caller to read a missing verdict as "nothing
# better is open" — the exact misreport this command exists to prevent.
UNANSWERED = frozenset({VERDICT_NO_HELD_SEAT, VERDICT_POSITION_UNKNOWN, VERDICT_EXIT_ROW_UNKNOWN})


def main(argv=None) -> int:
    result = run(parse_args(argv))
    print(json.dumps(result))
    if "error" in result or result.get("verdict") in UNANSWERED:
        print(f"expertflyer: {result.get('detail', result.get('error'))}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
