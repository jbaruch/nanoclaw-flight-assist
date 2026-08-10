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
