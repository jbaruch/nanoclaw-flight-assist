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
import sys
import urllib.error
import urllib.parse
import urllib.request
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
        return {
            "error": "unreachable",
            "detail": (
                f"ExpertFlyer API not reachable at {_base_url()} ({exc.reason}) — "
                f"check the service is running and {URL_ENV} points at it"
            ),
        }


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
    tiers = seat_quality.exit_tiers(result["seats"])
    ranked = seat_quality.rank_seats(result["seats"], cabin)
    result["ranked"] = [{**s, "why": seat_quality.describe(s, cabin, tiers)} for s in ranked]
    best = ranked[0] if ranked else None
    result["best"] = seat_quality.describe(best, cabin, tiers) if best else None
    # Every open seat may still be unacceptable — a cabin of middles ranks to
    # nothing even though `available_total` is non-zero.
    result["acceptable_total"] = len(ranked)
    return result


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


def run(args) -> dict:
    if args.action == "seats":
        result = _request(
            "GET",
            "/seats",
            {
                "airline": args.airline,
                "flight": args.flight,
                "date": args.date,
                "cabin": args.cabin,
                "want": args.want,
                "origin": args.origin,
                "destination": args.destination,
            },
        )
        return _rank(result)
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


def main(argv=None) -> int:
    result = run(parse_args(argv))
    print(json.dumps(result))
    if "error" in result:
        print(f"expertflyer: {result.get('detail', result['error'])}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
