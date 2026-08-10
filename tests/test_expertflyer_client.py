"""Contract of the ExpertFlyer API client.

Dates here are fixed PAST dates on purpose: the HTTP layer is mocked, so no
live upstream rejects a past-dated search and the Live-Upstream Future-Date
carve-out does not apply. A future literal would only rot.

The browser layer, the credential and every parsing rule live in the
`expertflyer-api` service and are tested there. What matters here is that this
container builds the right request, relays the service's verdict without
flattening it, and never mistakes a failure for a result.
"""

import email.message
import importlib.util
import json
import ssl
import urllib.error
import urllib.parse
from io import BytesIO
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relpath)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


client = _load("expertflyer_client", "skills/expertflyer/scripts/expertflyer.py")


class _Response:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def capture(monkeypatch):
    """Record the outgoing request and return a canned payload."""
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["method"] = request.get_method()
        seen["headers"] = dict(request.header_items())
        seen["body"] = json.loads(request.data.decode()) if request.data else None
        seen["timeout"] = timeout
        return _Response(seen.get("_payload", {"ok": True}))

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.delenv(client.TOKEN_ENV, raising=False)
    monkeypatch.setenv(client.URL_ENV, "http://service:8090")
    return seen


def test_seats_builds_the_query(capture):
    client.run(
        client.parse_args(
            [
                "seats",
                "--airline",
                "DL",
                "--flight",
                "2957",
                "--date",
                "2024-03-05",
                "--cabin",
                "comfort+",
                "--want",
                "non-middle",
            ]
        )
    )
    assert capture["url"].startswith("http://service:8090/seats?")
    assert "cabin=comfort%2B" in capture["url"]
    assert "want=non-middle" in capture["url"]
    assert capture["method"] == "GET"


def test_omitted_route_is_not_sent_as_empty(capture):
    """The service resolves the route itself; empty params would override that."""
    client.run(
        client.parse_args(
            ["seats", "--airline", "DL", "--flight", "2957", "--date", "2024-03-05", "--cabin", "W"]
        )
    )
    assert "origin=" not in capture["url"]
    assert "destination=" not in capture["url"]


def test_fare_class_sends_the_class_alias(capture):
    client.run(
        client.parse_args(
            [
                "fare-class",
                "--origin",
                "JFK",
                "--destination",
                "AMS",
                "--date",
                "2024-03-19",
                "--airline",
                "KL",
                "--class",
                "Z",
                "--flight",
                "642",
            ]
        )
    )
    assert "class=Z" in capture["url"]
    assert "flight=642" in capture["url"]


def test_create_alert_posts_a_json_body(capture):
    client.run(
        client.parse_args(
            [
                "create-alert",
                "--kind",
                "fare-class",
                "--airline",
                "KL",
                "--flight",
                "642",
                "--date",
                "2024-03-19",
                "--origin",
                "JFK",
                "--destination",
                "AMS",
                "--class",
                "Z",
            ]
        )
    )
    assert capture["method"] == "POST"
    assert capture["body"]["kind"] == "fare-class"
    assert capture["body"]["class"] == "Z"
    assert capture["body"]["force"] is False


def test_delete_alert_uses_the_id_path(capture):
    client.run(client.parse_args(["delete-alert", "--id", "5736694"]))
    assert capture["url"].endswith("/alerts/5736694")
    assert capture["method"] == "DELETE"


def test_bearer_is_sent_when_configured(capture, monkeypatch):
    monkeypatch.setenv(client.TOKEN_ENV, "s3cret")
    client.run(client.parse_args(["alerts"]))
    assert capture["headers"]["Authorization"] == "Bearer s3cret"


def test_no_bearer_header_without_a_token(capture):
    client.run(client.parse_args(["alerts"]))
    assert "Authorization" not in capture["headers"]


def _http_error(status, payload, monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url,
            status,
            "err",
            email.message.Message(),
            BytesIO(json.dumps(payload).encode()),
        )

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)


def test_expired_session_is_relayed_as_auth(monkeypatch):
    _http_error(503, {"detail": {"error": "auth", "detail": "session expired"}}, monkeypatch)
    result = client.run(client.parse_args(["alerts"]))
    assert result == {"error": "auth", "detail": "session expired"}


def test_bot_wall_stays_distinct_from_auth(monkeypatch):
    """Upstream 403s both ways; only the service can tell them apart."""
    _http_error(502, {"detail": {"error": "blocked", "detail": "bot wall"}}, monkeypatch)
    assert client.run(client.parse_args(["alerts"]))["error"] == "blocked"


def test_service_down_is_unreachable_not_a_result(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)
    result = client.run(client.parse_args(["alerts"]))
    assert result["error"] == "unreachable"
    assert client.URL_ENV in result["detail"]


def test_non_json_error_body_still_reports(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url,
            500,
            "err",
            email.message.Message(),
            BytesIO(b"<html>gateway blew up</html>"),
        )

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)
    result = client.run(client.parse_args(["alerts"]))
    assert result["error"] == "upstream"
    assert "gateway blew up" in result["detail"]


def test_main_exits_non_zero_on_error(monkeypatch, capsys):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)
    code = client.main(["alerts"])
    assert code == 1
    assert json.loads(capsys.readouterr().out)["error"] == "unreachable"


def test_main_exits_zero_on_success(capture, capsys):
    capture["_payload"] = {"count": 3, "alerts": []}
    assert client.main(["alerts"]) == 0
    assert json.loads(capsys.readouterr().out)["count"] == 3


def test_default_url_addresses_the_bridge_by_ip_not_the_bypassed_hostname():
    """host.docker.internal is in nanoclaw's AGENT_PROXY_BYPASS_HOSTS.

    Defaulting to that name would skip the OneCLI gateway, so the container's
    `onecli-managed` placeholder would go out unswapped and earn a 401.
    """
    assert client.DEFAULT_URL == "http://172.17.0.1:8090"
    assert "host.docker.internal" not in client.DEFAULT_URL


# --- ranking is applied to the seats response --------------------------------


LIVE_SEATS = {
    "flight": "DL2957",
    "route": "ATL-YYZ",
    "cabin": "W",
    "matching": [],
    "seats": [
        {
            "label": "14B",
            "row": 14,
            "column": "B",
            "position": "middle",
            "isExitRow": False,
            "isBulkhead": False,
            "cabin": "W",
        }
    ],
    "available_total": 1,
    "recommend_alert": True,
}


def test_seats_response_is_ranked_before_it_reaches_the_agent():
    """A cabin whose only free seat is a middle offers nothing."""
    out = client._rank(dict(LIVE_SEATS))
    assert out["ranked"] == []
    assert out["best"] is None
    assert out["acceptable_total"] == 0
    assert out["available_total"] == 1


def test_ranking_orders_bookable_seats_and_says_why():
    payload = {
        "cabin": "W",
        "seats": [
            {"label": "20C", "row": 20, "column": "C", "position": "aisle", "cabin": "W"},
            {"label": "12A", "row": 12, "column": "A", "position": "window", "cabin": "W"},
        ],
        "available_total": 2,
    }
    out = client._rank(payload)
    assert [s["label"] for s in out["ranked"]] == ["12A", "20C"]
    assert out["best"] == "12A (window)"
    assert out["acceptable_total"] == 2


def test_ranking_leaves_the_service_criteria_filter_alone():
    out = client._rank(dict(LIVE_SEATS))
    assert out["matching"] == []


def test_an_error_payload_is_passed_through_unranked():
    err = {"error": "auth", "detail": "session expired"}
    assert client._rank(dict(err)) == err


def test_a_response_without_seats_is_left_alone():
    """Older service versions return only `matching`; do not invent fields."""
    old = {"matching": ["12A"], "available_total": 1}
    assert client._rank(dict(old)) == old


def test_rank_labels_the_reclining_exit_row_on_the_production_path():
    """Service seats carry no recline field; the tier comes from adjacency.

    Exercises _rank() rather than describe() directly — the earlier test passed
    tiers by hand and so could not catch the production call omitting them.
    """
    payload = {
        "cabin": "Y",
        "seats": [
            {
                "label": "20A",
                "row": 20,
                "column": "A",
                "position": "window",
                "isExitRow": True,
                "cabin": "Y",
            },
            {
                "label": "21A",
                "row": 21,
                "column": "A",
                "position": "window",
                "isExitRow": True,
                "cabin": "Y",
            },
        ],
        "exit_rows": [20, 21],
        "available_total": 2,
    }
    out = client._rank(payload)
    assert out["best"] == "21A (window, exit row, reclines)"
    assert out["ranked"][0]["why"] == "21A (window, exit row, reclines)"
    # The row in front is fixed-back precisely because 21 sits behind it.
    assert out["ranked"][1]["why"] == "20A (window, exit row)"


def test_rank_does_not_promote_an_exit_row_when_the_layout_is_absent():
    """No `exit_rows` from the service: the rear row may be occupied."""
    payload = {
        "cabin": "Y",
        "seats": [
            {
                "label": "20A",
                "row": 20,
                "column": "A",
                "position": "window",
                "isExitRow": True,
                "cabin": "Y",
            },
        ],
        "available_total": 1,
    }
    assert client._rank(payload)["best"] == "20A (window, exit row)"


# --- the UTC-date fallback lives in the script, not in agent prose -----------


def test_previous_day_is_computed_not_string_sliced():
    assert client._previous_day("2024-03-01") == "2024-02-29"  # leap year
    assert client._previous_day("2024-01-01") == "2023-12-31"


def _seats_argv(date, *extra):
    return [
        "seats",
        "--airline",
        "DL",
        "--flight",
        "9",
        "--date",
        date,
        "--cabin",
        "W",
        *extra,
    ]


def test_a_flight_missing_on_the_utc_date_is_retried_on_the_previous_day(monkeypatch):
    """A late local departure lands on the next UTC day."""
    calls = []

    def fake(method, path, params=None, body=None):
        assert params is not None
        calls.append(params["date"])
        if params["date"] == "2024-03-07":
            return {"error": "error", "detail": "could not resolve a route for DL9"}
        return {"cabin": "W", "seats": [], "available_total": 0}

    monkeypatch.setattr(client, "_request", fake)
    out = client.run(client.parse_args(_seats_argv("2024-03-07", "--date-fallback")))
    assert calls == ["2024-03-07", "2024-03-06"]
    assert out["date_fallback_applied"] == "2024-03-06"


def test_the_fallback_is_off_unless_asked_for(monkeypatch):
    """A date the operator named is not a UTC-schedule artefact.

    Retrying it would answer about the previous day's flight and present those
    seats as the requested one's.
    """
    calls = []

    def fake(method, path, params=None, body=None):
        assert params is not None
        calls.append(params["date"])
        return {"error": "error", "detail": "could not resolve a route for DL9"}

    monkeypatch.setattr(client, "_request", fake)
    out = client.run(client.parse_args(_seats_argv("2024-03-07")))
    assert calls == ["2024-03-07"]
    assert out["error"] == "error"


def test_an_unrelated_error_is_not_retried(monkeypatch):
    """Only an unresolved route suggests the date; auth failures do not."""
    calls = []

    def fake(method, path, params=None, body=None):
        assert params is not None
        calls.append(params["date"])
        return {"error": "auth", "detail": "session expired"}

    monkeypatch.setattr(client, "_request", fake)
    client.run(client.parse_args(_seats_argv("2024-03-07", "--date-fallback")))
    assert calls == ["2024-03-07"]


def test_a_successful_first_try_is_not_retried(monkeypatch):
    calls = []

    def fake(method, path, params=None, body=None):
        assert params is not None
        calls.append(params["date"])
        return {"cabin": "W", "seats": [], "available_total": 0}

    monkeypatch.setattr(client, "_request", fake)
    out = client.run(client.parse_args(_seats_argv("2024-03-07")))
    assert calls == ["2024-03-07"]
    assert "date_fallback_applied" not in out


def test_a_failed_retry_reports_its_own_error_not_the_first_one(monkeypatch):
    """An expired session on the retry must not surface as "no such flight"."""

    def fake(method, path, params=None, body=None):
        assert params is not None
        if params["date"] == "2024-03-07":
            return {"error": "error", "detail": "could not resolve a route for DL9"}
        return {"error": "auth", "detail": "session expired"}

    monkeypatch.setattr(client, "_request", fake)
    out = client.run(client.parse_args(_seats_argv("2024-03-07", "--date-fallback")))
    assert out["error"] == "auth"
    assert out["detail"] == "session expired"
    assert out["date_fallback_attempted"] == "2024-03-06"
    assert "date_fallback_applied" not in out


def test_a_malformed_date_reports_rather_than_tracebacks(monkeypatch):
    """The retry must not crash computing the previous day."""

    def fake(method, path, params=None, body=None):
        assert params is not None
        return {"error": "error", "detail": "could not resolve a route for DL9"}

    monkeypatch.setattr(client, "_request", fake)
    out = client.run(client.parse_args(_seats_argv("next tuesday", "--date-fallback")))
    assert out["error"] == "bad_request"
    assert "YYYY-MM-DD" in out["detail"]


# --- a certificate failure is not an unreachable service ---------------------


def _url_error(reason, monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError(reason)

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)


def test_a_certificate_failure_is_reported_as_tls_not_unreachable(monkeypatch):
    """The service answered; only verification failed.

    Reporting "check the service is running" sends the operator to the wrong
    layer — the endpoint is fine and the trust store is not.
    """
    reason = ssl.SSLCertVerificationError("unable to get local issuer certificate")
    reason.verify_message = "unable to get local issuer certificate"
    reason.reason = "CERTIFICATE_VERIFY_FAILED"
    _url_error(reason, monkeypatch)

    result = client.run(client.parse_args(["alerts"]))
    assert result["error"] == "tls"
    assert "SSL_CERT_FILE" in result["detail"]
    assert "check the service is running" not in result["detail"]
    # The recovery must not prescribe a package this plugin does not depend on:
    # the client is stdlib-only, so `python3 -m certifi` can fail with
    # ModuleNotFoundError on the very host that needs the fix.
    # Match the package invocation, not the bare word: "certificate" contains
    # "certifi" and would false-positive.
    assert "-m certifi" not in result["detail"]
    assert "/etc/ssl/cert.pem" in result["detail"]


def test_a_refused_connection_is_still_unreachable(monkeypatch):
    """The ordinary case must keep pointing at the service and the URL."""
    _url_error(ConnectionRefusedError(61, "Connection refused"), monkeypatch)
    result = client.run(client.parse_args(["alerts"]))
    assert result["error"] == "unreachable"
    assert client.URL_ENV in result["detail"]


def test_a_tls_failure_still_exits_non_zero(monkeypatch, capsys):
    reason = ssl.SSLCertVerificationError("bad chain")
    reason.verify_message = "bad chain"
    reason.reason = "CERTIFICATE_VERIFY_FAILED"
    _url_error(reason, monkeypatch)
    assert client.main(["alerts"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "tls"


# --- a seat the ranker refuses is reported, not raised ------------------------


def test_an_unrankable_seat_is_reported_as_an_error_not_a_traceback():
    """The service answered; one seat could not be ordered. The operator has to
    read WHICH seat, so it is a structured error rather than a stack trace."""
    payload = {
        "cabin": "W",
        "seats": [{"label": "12A", "row": 12, "column": "A", "position": "porthole", "cabin": "W"}],
        "available_total": 1,
    }
    out = client._rank(payload)
    assert out["error"] == "unrankable"
    assert "12A" in out["detail"]
    # A partial ranking would read as a complete one.
    assert "ranked" not in out
    assert "best" not in out


def test_an_unknown_cabin_is_reported_rather_than_ranked_as_main():
    payload = {
        "cabin": "P",
        "seats": [{"label": "2A", "row": 2, "column": "A", "position": "window", "cabin": "P"}],
        "available_total": 1,
    }
    out = client._rank(payload)
    assert out["error"] == "unrankable"
    assert "'P'" in out["detail"]
    # Step 6 relays `detail` and promises it names the seat.
    assert "2A" in out["detail"]


def test_an_unrankable_response_exits_non_zero(capture, capsys):
    capture["_payload"] = {
        "cabin": "W",
        "seats": [{"label": "12A", "row": 12, "column": "A", "position": "porthole", "cabin": "W"}],
        "available_total": 1,
    }
    code = client.main(
        ["seats", "--airline", "DL", "--flight", "2957", "--date", "2024-03-05", "--cabin", "W"]
    )
    assert code == 1
    assert "porthole" in capsys.readouterr().err


# --- assess: the held seat versus everything open ----------------------------


@pytest.fixture
def cabins(monkeypatch):
    """Answer each /seats call with the payload for the cabin it asked about.

    `assess` sweeps more than one cabin, so a single canned response cannot
    express the case it exists for: a better cabin holding the upgrade.
    """
    by_cabin: dict[str, dict] = {}
    asked: list[str] = []

    def fake_urlopen(request, timeout=None):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)
        cabin = query["cabin"][0]
        asked.append(cabin)
        return _Response(by_cabin.get(cabin, {"cabin_present": False, "cabin": cabin}))

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv(client.URL_ENV, "http://service:8090")
    by_cabin["_asked"] = asked  # type: ignore[assignment]
    return by_cabin


def _cabin(code, seats, *, exit_rows=None, present=True):
    payload = {
        "cabin": code,
        "cabin_present": present,
        "seats": [{**s, "cabin": code} for s in seats],
        "available_total": len(seats),
        "flight": "DL2957",
        "route": "ATL-YYZ",
    }
    if exit_rows is not None:
        payload["exit_rows"] = exit_rows
    return payload


def _assess(**overrides):
    argv = [
        "assess",
        "--airline",
        "DL",
        "--flight",
        "2957",
        "--date",
        "2024-03-05",
        "--held-cabin",
        "W",
    ]
    for flag, value in overrides.items():
        if value is None:
            continue
        argv += [f"--{flag.replace('_', '-')}", str(value)]
    return client.run(client.parse_args(argv))


def test_assess_without_a_held_seat_refuses_to_answer(cabins):
    """The failure this command exists to prevent: reporting on open seats as
    though it had judged the operator's own."""
    out = _assess()
    assert out["verdict"] == client.VERDICT_NO_HELD_SEAT
    assert "--held" in out["detail"]
    assert "upgrades" not in out
    assert cabins["_asked"] == []


def test_assess_without_a_held_seat_exits_non_zero(capsys, cabins):
    code = client.main(
        [
            "assess",
            "--airline",
            "DL",
            "--flight",
            "2957",
            "--date",
            "2024-03-05",
            "--held-cabin",
            "W",
        ]
    )
    assert code == 1
    assert "--held" in capsys.readouterr().err


def test_a_window_stays_optimal_when_only_middles_are_open(cabins):
    """The live DL2957 case: 21F held, 13B the one open Comfort+ seat."""
    cabins["W"] = _cabin("W", [{"label": "13B", "row": 13, "column": "B", "position": "middle"}])
    cabins["A"] = _cabin("A", [], present=False)
    out = _assess(held="21F", held_position="window")
    assert out["verdict"] == client.VERDICT_OPTIMAL
    assert out["upgrades"] == []
    assert out["best_upgrade"] is None
    assert out["alert_recommended"] is True
    assert out["cabins_absent"] == ["A"]


def test_a_comfort_plus_window_never_upgrades_the_first_seat_held(cabins):
    """1A on DL2714, with Comfort+ open further back. The ladder decides."""
    cabins["F"] = _cabin(
        "F",
        [
            {"label": "3C", "row": 3, "column": "C", "position": "aisle"},
            # Row 1 seen in F corroborates the held cabin, so no downward probe.
            {"label": "1C", "row": 1, "column": "C", "position": "aisle"},
        ],
    )
    out = client.run(
        client.parse_args(
            [
                "assess",
                "--airline",
                "DL",
                "--flight",
                "2714",
                "--date",
                "2024-03-05",
                "--held-cabin",
                "first",
                "--held",
                "1A",
                "--held-position",
                "window",
                "--scan-up",
                "0",
            ]
        )
    )
    assert out["verdict"] == client.VERDICT_OPTIMAL
    assert cabins["_asked"] == ["F"]


def test_the_held_position_is_read_off_the_seat_map_when_not_stated(cabins):
    cabins["W"] = _cabin(
        "W",
        [
            {"label": "25F", "row": 25, "column": "F", "position": "window"},
            {"label": "14B", "row": 14, "column": "B", "position": "middle"},
        ],
    )
    out = _assess(held="21F")
    assert out["held"]["position"] == "window"
    assert out["held"]["position_source"] == "seat-map"
    # 25F is the same column four rows back, so it settles what F is without
    # being worth moving to.
    assert out["verdict"] == client.VERDICT_OPTIMAL


def test_a_stated_position_is_not_overridden_by_the_seat_map(cabins):
    cabins["W"] = _cabin("W", [{"label": "12F", "row": 12, "column": "F", "position": "window"}])
    out = _assess(held="21F", held_position="aisle")
    assert out["held"]["position"] == "aisle"
    assert out["held"]["position_source"] == "stated"


def test_an_underivable_position_is_reported_rather_than_guessed(cabins):
    """No open seat shares the column, so the seat map says nothing about it."""
    cabins["W"] = _cabin("W", [{"label": "14B", "row": 14, "column": "B", "position": "middle"}])
    out = _assess(held="21F")
    assert out["verdict"] == client.VERDICT_POSITION_UNKNOWN
    assert "--held-position" in out["detail"]
    assert "upgrades" not in out


def test_an_underivable_position_exits_non_zero(capsys, cabins):
    cabins["W"] = _cabin("W", [{"label": "14B", "row": 14, "column": "B", "position": "middle"}])
    code = client.main(
        [
            "assess",
            "--airline",
            "DL",
            "--flight",
            "2957",
            "--date",
            "2024-03-05",
            "--held-cabin",
            "W",
            "--held",
            "21F",
        ]
    )
    assert code == 1
    assert "--held-position" in capsys.readouterr().err


def test_the_held_exit_row_comes_from_the_cabin_layout(cabins):
    """21F is an exit row, so a plain window further forward does not beat it."""
    cabins["W"] = _cabin(
        "W",
        [{"label": "12A", "row": 12, "column": "A", "position": "window"}],
        exit_rows=[21],
    )
    out = _assess(held="21F", held_position="window")
    assert out["held"]["isExitRow"] is True
    assert out["verdict"] == client.VERDICT_OPTIMAL
    assert "exit row" in out["held"]["why"]


def test_a_cabin_that_failed_to_load_is_not_reported_as_nothing_better(cabins):
    """A cabin that errored could be holding the upgrade."""
    cabins["W"] = _cabin("W", [])
    cabins["A"] = {"error": "blocked", "detail": "bot wall"}
    out = _assess(held="21F", held_position="window")
    assert out["error"] == "blocked"
    assert out["cabin_failed"] == "A"
    assert "verdict" not in out


def test_a_held_cabin_the_aircraft_lacks_is_a_bad_request(cabins):
    cabins["W"] = _cabin("W", [], present=False)
    out = _assess(held="21F", held_position="window")
    assert out["error"] == "bad_request"
    assert "no W cabin" in out["detail"]


def test_an_unknown_held_cabin_never_reaches_the_service(cabins):
    out = client.run(
        client.parse_args(
            [
                "assess",
                "--airline",
                "DL",
                "--flight",
                "2957",
                "--date",
                "2024-03-05",
                "--held-cabin",
                "sky club",
                "--held",
                "21F",
            ]
        )
    )
    assert out["error"] == "bad_request"
    assert cabins["_asked"] == []


def test_scan_width_controls_how_many_cabins_are_requested(cabins):
    # Row 21 seen in W corroborates the held cabin, so the width is the only
    # thing deciding how many requests go out.
    cabins["W"] = _cabin("W", [{"label": "21B", "row": 21, "column": "B", "position": "middle"}])
    out = _assess(held="21F", held_position="window", scan_up=0)
    assert cabins["_asked"] == ["W"]
    assert out["cabins_scanned"] == ["W"]


def test_an_unanswerable_position_still_names_the_seat_and_cabin(cabins):
    """The operator is asked about a specific seat, so the refusal identifies
    it rather than reporting a bare column."""
    cabins["W"] = _cabin("W", [{"label": "14B", "row": 14, "column": "B", "position": "middle"}])
    held = _assess(held="21F")["held"]
    assert held["label"] == "21F"
    assert held["cabin"] == "W"
    assert held["position"] is None
    assert held["position_source"] is None


def test_a_held_middle_is_assessed_rather_than_crashing(cabins):
    """`--held-position middle` is a supported input, and the operator stuck in
    one is the case most worth answering."""
    cabins["W"] = _cabin("W", [{"label": "20C", "row": 20, "column": "C", "position": "aisle"}])
    out = _assess(held="13B", held_position="middle")
    assert out["verdict"] == client.VERDICT_UPGRADE
    assert out["best_upgrade"] == "20C (aisle)"
    assert out["held"]["position"] == "middle"


def test_a_held_middle_with_only_middles_open_stays_optimal(cabins):
    """Nothing worth taking is open, so there is nothing to move to — the
    verdict is about the open seats, not an endorsement of the middle."""
    cabins["W"] = _cabin("W", [{"label": "14B", "row": 14, "column": "B", "position": "middle"}])
    out = _assess(held="13B", held_position="middle")
    assert out["verdict"] == client.VERDICT_OPTIMAL
    assert out["alert_recommended"] is True


def test_optimal_names_the_cabins_it_never_looked_at(cabins):
    """A one-rung sweep from Main cannot see Business, so `optimal` scoped to
    the whole aircraft would be a false verdict."""
    # One open seat that loses to the held one, so the sweep has an evidence
    # base and the verdict is a real comparison rather than an empty one.
    cabins["Y"] = _cabin("Y", [{"label": "40B", "row": 40, "column": "B", "position": "middle"}])
    cabins["W"] = _cabin("W", [])
    out = client.run(
        client.parse_args(
            [
                "assess",
                "--airline",
                "DL",
                "--flight",
                "2957",
                "--date",
                "2024-03-05",
                "--held-cabin",
                "Y",
                "--held",
                "30A",
                "--held-position",
                "window",
            ]
        )
    )
    assert out["verdict"] == client.VERDICT_OPTIMAL
    assert out["cabins_scanned"] == ["W", "Y"]
    assert out["cabins_unscanned"] == ["F", "C", "A"]


def test_a_sweep_reaching_the_top_leaves_nothing_unscanned(cabins):
    cabins["F"] = _cabin("F", [{"label": "4B", "row": 4, "column": "B", "position": "middle"}])
    out = client.run(
        client.parse_args(
            [
                "assess",
                "--airline",
                "DL",
                "--flight",
                "2714",
                "--date",
                "2024-03-05",
                "--held-cabin",
                "first",
                "--held",
                "1A",
                "--held-position",
                "window",
            ]
        )
    )
    assert out["cabins_unscanned"] == []


# --- the sweep needs something to compare against ----------------------------


def test_a_sweep_that_saw_no_open_seat_reports_that_not_optimal(cabins):
    """The live DL2957 case: Comfort+ sold out, Premium Select not on the
    aircraft, and `optimal` reported off zero observed seats."""
    cabins["W"] = _cabin("W", [])
    cabins["A"] = _cabin("A", [], present=False)
    out = _assess(held="21F", held_position="window")
    assert out["verdict"] == client.VERDICT_NOTHING_OPEN
    assert out["seats_compared"] == 0
    assert "nothing was compared" in out["detail"]
    assert "upgrades" not in out
    assert "alert_recommended" not in out


def test_nothing_open_exits_non_zero(capsys, cabins):
    cabins["W"] = _cabin("W", [])
    code = client.main(
        [
            "assess",
            "--airline",
            "DL",
            "--flight",
            "2957",
            "--date",
            "2024-03-05",
            "--held-cabin",
            "W",
            "--held",
            "21F",
            "--held-position",
            "window",
        ]
    )
    assert code == 1
    assert "nothing was compared" in capsys.readouterr().err


def test_one_open_seat_is_evidence_enough_to_answer(cabins):
    cabins["W"] = _cabin("W", [{"label": "40B", "row": 40, "column": "B", "position": "middle"}])
    out = _assess(held="21F", held_position="window")
    assert out["verdict"] == client.VERDICT_OPTIMAL
    assert out["seats_compared"] == 1


# --- the held seat is not in the cabin it was said to be in ------------------


def test_a_corroborated_cabin_says_so(cabins):
    cabins["W"] = _cabin("W", [{"label": "21B", "row": 21, "column": "B", "position": "middle"}])
    out = _assess(held="21F", held_position="window")
    assert out["held_cabin_corroborated"] is True
    assert "cabins_probed" not in out
    assert "cabin_probe_failed" not in out


def test_the_pre_assessment_shapes_carry_no_verdict_or_held(cabins):
    """Step 3 documents three responses that precede any assessment. An agent
    reading them as malformed is the failure this pins."""
    bad_cabin = client.run(
        client.parse_args(
            [
                "assess",
                "--airline",
                "DL",
                "--flight",
                "2957",
                "--date",
                "2024-03-05",
                "--held-cabin",
                "sky club",
                "--held",
                "21F",
            ]
        )
    )
    assert bad_cabin["error"] == "bad_request"
    assert "verdict" not in bad_cabin and "held" not in bad_cabin

    cabins["W"] = {"error": "blocked", "detail": "bot wall"}
    failed = _assess(held="21F", held_position="window")
    assert failed["cabin_failed"] == "A" or failed["cabin_failed"] == "W"
    assert "verdict" not in failed and "held" not in failed

    none_held = _assess()
    assert none_held["verdict"] == client.VERDICT_NO_HELD_SEAT
    assert "held" not in none_held and "cabins_scanned" not in none_held


def test_the_aircraft_lacking_the_held_cabin_reports_which_were_absent(cabins):
    cabins["W"] = _cabin("W", [], present=False)
    out = _assess(held="21F", held_position="window")
    assert out["error"] == "bad_request"
    assert "W" in out["cabins_absent"] or out["cabins_absent"] == ["A", "W"]
    assert "verdict" not in out


# --- the held seat's cabin: corroborate, never disprove -----------------------


def test_a_row_seen_in_the_held_cabin_corroborates_it(cabins):
    cabins["W"] = _cabin("W", [{"label": "21B", "row": 21, "column": "B", "position": "middle"}])
    out = _assess(held="21F", held_position="window")
    assert out["held_cabin_corroborated"] is True
    assert "row_seen_in" not in out


def test_a_row_absent_from_the_held_cabin_is_never_disproof(cabins):
    """`/seats` reports bookable seats, so a row whose every seat is taken is
    missing from the response while still being in the cabin. Refusing on that
    would reject a correct assessment."""
    cabins["W"] = _cabin("W", [{"label": "12A", "row": 12, "column": "A", "position": "window"}])
    out = _assess(held="21F", held_position="window")
    assert out["held_cabin_corroborated"] is None
    assert out["row_seen_in"] == []
    # Still a real verdict — absence of evidence is not a refusal.
    assert out["verdict"] == client.VERDICT_UPGRADE


def test_a_row_seen_elsewhere_is_a_hint_not_a_verdict(cabins):
    """Cabins split mid-row — the 739's Comfort+ ends a row later on the right
    — so one row number legitimately appears in two cabins."""
    cabins["W"] = _cabin("W", [{"label": "12A", "row": 12, "column": "A", "position": "window"}])
    cabins["A"] = _cabin("A", [{"label": "21D", "row": 21, "column": "D", "position": "aisle"}])
    out = _assess(held="21F", held_position="window")
    assert out["held_cabin_corroborated"] is None
    assert out["row_seen_in"] == ["A"]
    assert out["verdict"] in (client.VERDICT_OPTIMAL, client.VERDICT_UPGRADE)


def test_every_shape_carrying_a_held_seat_describes_it(cabins):
    """Step 3 promises `held.why` on every shape but `held_position_unknown`."""
    cabins["W"] = _cabin("W", [], present=True)
    nothing = _assess(held="21F", held_position="window")
    assert nothing["verdict"] == client.VERDICT_NOTHING_OPEN
    assert nothing["held"]["why"] == "21F (window)"

    cabins["W"] = _cabin("W", [{"label": "12A", "row": 12, "column": "A", "position": "window"}])
    upgrade = _assess(held="21F", held_position="window")
    assert upgrade["held"]["why"] == "21F (window)"

    cabins["W"] = _cabin("W", [{"label": "14B", "row": 14, "column": "B", "position": "middle"}])
    unknown = _assess(held="21F")
    assert unknown["verdict"] == client.VERDICT_POSITION_UNKNOWN
    assert "why" not in unknown["held"]


# --- cabin membership, decided from the cabin's row extent -------------------


def _cabin_with_rows(code, seats, rows, *, exit_rows=None):
    payload = _cabin(code, seats, exit_rows=exit_rows)
    payload["rows"] = rows
    return payload


def test_a_row_outside_the_held_cabin_s_extent_is_a_mismatch(cabins):
    """The live DL2957 case, now decidable: Comfort+ runs 10-20, so 21F is not
    in it however sold out the cabin is."""
    cabins["W"] = _cabin_with_rows("W", [], list(range(10, 21)))
    cabins["A"] = _cabin("A", [], present=False)
    out = _assess(held="21F", held_position="window")
    assert out["verdict"] == client.VERDICT_CABIN_MISMATCH
    assert out["held_cabin_corroborated"] is False
    assert out["held_cabin_source"] == "rows"
    assert "rows 10-20" in out["detail"]
    assert "upgrades" not in out
    # Step 3 promises `held.why` on every shape but `held_position_unknown`.
    assert out["held"]["why"] == "21F (window)"


def test_the_mismatch_names_the_cabin_the_row_belongs_to(cabins):
    cabins["W"] = _cabin_with_rows("W", [], list(range(10, 21)))
    cabins["A"] = _cabin_with_rows("A", [], list(range(21, 40)))
    out = _assess(held="21F", held_position="window")
    assert out["verdict"] == client.VERDICT_CABIN_MISMATCH
    assert "it is in A" in out["detail"]


def test_a_sold_out_cabin_still_confirms_membership(cabins):
    """`rows` is layout, so an empty `seats` no longer means unknown — the case
    the seat-derived check could never settle."""
    cabins["W"] = _cabin_with_rows("W", [], list(range(10, 22)))
    out = _assess(held="21F", held_position="window")
    assert out["held_cabin_corroborated"] is True
    assert out["held_cabin_source"] == "rows"
    assert out["verdict"] == client.VERDICT_NOTHING_OPEN
    assert "row_seen_in" not in out


def test_an_occupied_row_is_not_mistaken_for_an_absent_one(cabins):
    """Every seat in row 21 taken, and the row is still in the cabin. The
    seat-derived check called this unknown; `rows` calls it what it is."""
    cabins["W"] = _cabin_with_rows(
        "W",
        [{"label": "12A", "row": 12, "column": "A", "position": "window"}],
        list(range(10, 22)),
    )
    out = _assess(held="21F", held_position="window")
    assert out["held_cabin_corroborated"] is True
    assert out["verdict"] == client.VERDICT_UPGRADE


def test_a_service_without_rows_still_corroborates_and_never_disproves(cabins):
    """Older expertflyer-api: no `rows`, so absence stays undecidable."""
    cabins["W"] = _cabin("W", [{"label": "12A", "row": 12, "column": "A", "position": "window"}])
    out = _assess(held="21F", held_position="window")
    assert out["held_cabin_corroborated"] is None
    assert out["held_cabin_source"] == "seats"
    assert out["verdict"] == client.VERDICT_UPGRADE


# --- why the held seat won, per cabin ----------------------------------------


def test_the_true_reason_per_cabin_is_reported(cabins):
    """The live DL2957 report claimed 21F "beat even Comfort+". It did not:
    Comfort+ outranks a Main exit row, and only had nothing acceptable open.
    `acceptable_by_cabin` is that distinction as data."""
    cabins["Y"] = _cabin_with_rows(
        "Y",
        [
            {"label": "21A", "row": 21, "column": "A", "position": "window", "isExitRow": True},
            {"label": "19B", "row": 19, "column": "B", "position": "middle", "isExitRow": True},
        ],
        list(range(16, 33)),
        exit_rows=[19, 20, 21],
    )
    # Comfort+ open but every seat a middle: nothing worth taking.
    cabins["W"] = _cabin_with_rows(
        "W", [{"label": "14B", "row": 14, "column": "B", "position": "middle"}], list(range(10, 21))
    )
    out = client.run(
        client.parse_args(
            [
                "assess",
                "--airline",
                "DL",
                "--flight",
                "2957",
                "--date",
                "2024-03-05",
                "--held-cabin",
                "Y",
                "--held",
                "21F",
                "--held-position",
                "window",
            ]
        )
    )
    assert out["verdict"] == client.VERDICT_OPTIMAL
    # W had nothing worth taking — not "W lost to 21F".
    assert out["acceptable_by_cabin"]["W"] == 0
    assert out["acceptable_by_cabin"]["Y"] == 1
    assert out["alert_recommended"] is True


def test_a_better_cabin_is_an_opening_not_a_seat_change(cabins):
    """A seat in a cabin the operator is not ticketed into cannot be selected
    in the app — it is a fare change or an upgrade clearance. Reporting it as
    an upgrade tells them to go take a seat that is not theirs to take."""
    cabins["Y"] = _cabin("Y", [{"label": "30C", "row": 30, "column": "C", "position": "aisle"}])
    cabins["W"] = _cabin("W", [{"label": "12A", "row": 12, "column": "A", "position": "window"}])
    out = client.run(
        client.parse_args(
            [
                "assess",
                "--airline",
                "DL",
                "--flight",
                "2957",
                "--date",
                "2024-03-05",
                "--held-cabin",
                "Y",
                "--held",
                "30A",
                "--held-position",
                "window",
            ]
        )
    )
    assert out["verdict"] == client.VERDICT_OPTIMAL
    assert out["upgrades"] == []
    assert out["best_upgrade"] is None
    assert [s["why"] for s in out["cabin_openings"]] == ["12A (window)"]


def test_a_same_cabin_seat_is_still_a_real_upgrade(cabins):
    """The counterpart: inside the held cabin, an open seat is selectable."""
    cabins["Y"] = _cabin(
        "Y",
        [
            {"label": "12A", "row": 12, "column": "A", "position": "window"},
            {"label": "30C", "row": 30, "column": "C", "position": "aisle"},
        ],
    )
    cabins["W"] = _cabin("W", [])
    out = client.run(
        client.parse_args(
            [
                "assess",
                "--airline",
                "DL",
                "--flight",
                "2957",
                "--date",
                "2024-03-05",
                "--held-cabin",
                "Y",
                "--held",
                "30A",
                "--held-position",
                "window",
            ]
        )
    )
    assert out["verdict"] == client.VERDICT_UPGRADE
    assert out["best_upgrade"] == "12A (window)"
    assert out["cabin_openings"] == []


def test_the_alert_covers_one_rung_up_however_wide_the_sweep(cabins):
    """The live complaint: a sweep widened to First offered an alert on First.
    A wide sweep sees what exists; it does not widen what is worth watching."""
    for code in ("Y", "W", "A", "C", "F"):
        cabins[code] = _cabin(code, [])
    # A middle only: nothing worth taking anywhere, so every watchable cabin
    # stays watchable and the rung bound is the only thing narrowing the list.
    cabins["Y"] = _cabin("Y", [{"label": "30B", "row": 30, "column": "B", "position": "middle"}])
    out = client.run(
        client.parse_args(
            [
                "assess",
                "--airline",
                "DL",
                "--flight",
                "2957",
                "--date",
                "2024-03-05",
                "--held-cabin",
                "Y",
                "--held",
                "30A",
                "--held-position",
                "window",
                "--scan-up",
                "4",
            ]
        )
    )
    assert out["cabins_scanned"] == ["F", "C", "A", "W", "Y"]
    assert out["alert_cabins"] == ["W", "Y"]


def test_the_alert_never_names_a_cabin_the_aircraft_lacks(cabins):
    cabins["W"] = _cabin("W", [{"label": "12B", "row": 12, "column": "B", "position": "middle"}])
    cabins["A"] = _cabin("A", [], present=False)
    out = _assess(held="21F", held_position="window")
    assert out["cabins_absent"] == ["A"]
    assert out["alert_cabins"] == ["W"]


def test_a_cabin_already_holding_a_seat_is_not_worth_watching(cabins):
    """Check first, alert only if absent. A watch on a cabin with a seat
    already open fires the moment it is created."""
    # Main has open seats, none better than the held exit row. Comfort+ empty.
    cabins["Y"] = _cabin(
        "Y",
        [{"label": "30C", "row": 30, "column": "C", "position": "aisle"}],
        exit_rows=[21],
    )
    cabins["W"] = _cabin("W", [])
    out = client.run(
        client.parse_args(
            [
                "assess",
                "--airline",
                "DL",
                "--flight",
                "2957",
                "--date",
                "2024-03-05",
                "--held-cabin",
                "Y",
                "--held",
                "21F",
                "--held-position",
                "window",
            ]
        )
    )
    assert out["verdict"] == client.VERDICT_OPTIMAL
    # Y already has a non-middle open, so a Y watch would fire immediately.
    assert out["acceptable_by_cabin"]["Y"] == 1
    assert out["alert_cabins"] == ["W"]
    assert out["alert_recommended"] is True


def test_nothing_is_recommended_when_every_watchable_cabin_has_a_seat(cabins):
    cabins["Y"] = _cabin(
        "Y", [{"label": "30C", "row": 30, "column": "C", "position": "aisle"}], exit_rows=[21]
    )
    cabins["W"] = _cabin("W", [{"label": "12B", "row": 12, "column": "B", "position": "middle"}])
    cabins["W"]["seats"].append(
        {"label": "14D", "row": 14, "column": "D", "position": "aisle", "cabin": "W"}
    )
    out = client.run(
        client.parse_args(
            [
                "assess",
                "--airline",
                "DL",
                "--flight",
                "2957",
                "--date",
                "2024-03-05",
                "--held-cabin",
                "Y",
                "--held",
                "21F",
                "--held-position",
                "window",
            ]
        )
    )
    assert out["alert_cabins"] == []
    assert out["alert_recommended"] is False


# --- the cabin is resolved, not asked for ------------------------------------


def test_the_cabin_is_read_off_the_aircraft_when_not_stated(cabins):
    """byAir stores the seat and no cabin the assessment can use, so the
    operator would otherwise have to look it up. `rows` already knows."""
    cabins["Y"] = _cabin_with_rows(
        "Y",
        [{"label": "22A", "row": 22, "column": "A", "position": "window"}],
        list(range(16, 33)),
        exit_rows=[19, 20, 21],
    )
    cabins["W"] = _cabin_with_rows("W", [], list(range(10, 16)))
    out = client.run(
        client.parse_args(
            [
                "assess",
                "--airline",
                "DL",
                "--flight",
                "2957",
                "--date",
                "2024-03-05",
                "--held",
                "21F",
                "--held-position",
                "window",
            ]
        )
    )
    assert out["held"]["cabin"] == "Y"
    assert out["held_cabin_from"] == "resolved"
    assert out["held_cabin_corroborated"] is True
    assert out["verdict"] == client.VERDICT_OPTIMAL
    # Main first — where most seats are — so the common case costs one request
    # before the sweep, and the sweep reuses it.
    assert cabins["_asked"] == ["Y", "W"]


def test_resolution_walks_up_until_it_finds_the_row(cabins):
    cabins["Y"] = _cabin_with_rows("Y", [], list(range(16, 33)))
    cabins["W"] = _cabin_with_rows("W", [], list(range(10, 16)))
    cabins["A"] = _cabin_with_rows(
        "A", [{"label": "6A", "row": 6, "column": "A", "position": "window"}], list(range(6, 10))
    )
    out = client.run(
        client.parse_args(
            [
                "assess",
                "--airline",
                "DL",
                "--flight",
                "2957",
                "--date",
                "2024-03-05",
                "--held",
                "7C",
                "--held-position",
                "aisle",
            ]
        )
    )
    assert out["held"]["cabin"] == "A"
    assert out["held_cabin_from"] == "resolved"
    assert cabins["_asked"][:3] == ["Y", "W", "A"]


def test_a_stated_cabin_skips_resolution_entirely(cabins):
    cabins["W"] = _cabin_with_rows("W", [], list(range(10, 21)))
    out = _assess(held="12F", held_position="window")
    assert out["held_cabin_from"] == "stated"
    assert "Y" not in cabins["_asked"]


def test_a_seat_on_no_cabin_of_this_aircraft_is_reported(cabins):
    """Row 88 is nobody's row: the seat or the flight is wrong."""
    cabins["Y"] = _cabin_with_rows("Y", [], list(range(16, 33)))
    cabins["W"] = _cabin_with_rows("W", [], list(range(10, 16)))
    out = client.run(
        client.parse_args(
            [
                "assess",
                "--airline",
                "DL",
                "--flight",
                "2957",
                "--date",
                "2024-03-05",
                "--held",
                "88A",
                "--held-position",
                "window",
            ]
        )
    )
    assert out["verdict"] == client.VERDICT_CABIN_UNRESOLVED
    assert "no cabin on this aircraft has a row 88" in out["detail"]
    assert "upgrades" not in out


def test_resolution_needs_the_service_to_report_rows(cabins):
    """An older expertflyer-api cannot answer this, and guessing the cabin is
    the defect the resolution exists to remove."""
    cabins["Y"] = _cabin("Y", [{"label": "22A", "row": 22, "column": "A", "position": "window"}])
    out = client.run(
        client.parse_args(
            [
                "assess",
                "--airline",
                "DL",
                "--flight",
                "2957",
                "--date",
                "2024-03-05",
                "--held",
                "21F",
                "--held-position",
                "window",
            ]
        )
    )
    assert out["error"] == "bad_request"
    assert "--held-cabin" in out["detail"]
