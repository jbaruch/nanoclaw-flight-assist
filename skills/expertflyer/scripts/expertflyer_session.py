"""Authenticated ExpertFlyer browser session.

ExpertFlyer publishes no API, so every capability here drives the authenticated
web UI. Two facts shape this module:

1. Stealth is mandatory. A vanilla headless context gets HTTP 403 on every
   request — including unauthenticated ones — so the bot wall and an expired
   session are indistinguishable by status code alone. The stealth config from
   jbaruch/fifty-tabs-of-fares clears the wall; auth is then decided by whether
   the page redirects to auth.expertflyer.com.
2. The session is a captured `storage_state` (Auth0, ~7-day life). The
   container holds no ExpertFlyer password.

Set EXPERTFLYER_STORAGE_STATE to the storage_state JSON path.
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

AUTH_HOST = "auth.expertflyer.com"
STATE_ENV = "EXPERTFLYER_STORAGE_STATE"
FIFTY_TABS_ENV = "FIFTY_TABS_SRC"
DEFAULT_FIFTY_TABS_SRC = "/opt/fifty-tabs-of-fares/src"


class ExpertFlyerError(RuntimeError):
    """Base for failures the skill reports as a structured error."""

    kind = "error"


class AuthError(ExpertFlyerError):
    """The stored session is missing or expired."""

    kind = "auth"


class BlockedError(ExpertFlyerError):
    """The bot wall rejected the request. Never retry in a loop."""

    kind = "blocked"


def _stealth_factories():
    """Import the stealth context builders, failing with an actionable message."""
    src = os.environ.get(FIFTY_TABS_ENV, DEFAULT_FIFTY_TABS_SRC)
    if src not in sys.path:
        sys.path.insert(0, src)
    try:
        from fifty_tabs.browser import create_browser, create_stealth_context
    except ImportError as exc:
        raise ExpertFlyerError(
            f"fifty-tabs stealth layer not importable from {src!r} — install "
            f"jbaruch/fifty-tabs-of-fares or set {FIFTY_TABS_ENV} to its src/ "
            "directory. Without it every ExpertFlyer request returns 403."
        ) from exc
    return create_browser, create_stealth_context


def _login_help() -> str:
    """How to get a session, phrased for whichever mode is configured."""
    from expertflyer_login import EMAIL_ENV, PASSWORD_ENV

    return (
        f"set {EMAIL_ENV} and {PASSWORD_ENV} so the skill can log in itself, or "
        "log in once in a headed browser and save the context's storage_state"
    )


async def ensure_state() -> None:
    """Make sure a session file exists, logging in when it does not.

    Credentials turn the ~7-day expiry into a self-healing condition instead of
    a weekly manual re-capture that always lapses mid-trip.
    """
    from expertflyer_login import credentials_available, login_and_save

    raw = os.environ.get(STATE_ENV)
    if not raw:
        raise AuthError(f"{STATE_ENV} is unset — {_login_help()}.")
    if Path(raw).is_file():
        return
    if not credentials_available():
        raise AuthError(f"{STATE_ENV}={raw!r} does not exist — {_login_help()}.")
    await login_and_save(raw)


def load_storage_state() -> dict:
    """Read the captured session, or explain how to produce one."""
    raw = os.environ.get(STATE_ENV)
    if not raw:
        raise AuthError(f"{STATE_ENV} is unset — {_login_help()}.")
    path = Path(raw)
    if not path.is_file():
        raise AuthError(f"{STATE_ENV}={raw!r} does not exist — {_login_help()}.")
    try:
        state = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise AuthError(f"{raw!r} is not valid JSON — recapture the session.") from exc
    if not state.get("cookies"):
        raise AuthError(f"{raw!r} holds no cookies — recapture the session.")
    return state


@asynccontextmanager
async def session_page():
    """Yield a stealth page carrying the stored ExpertFlyer session."""
    from playwright.async_api import async_playwright

    create_browser, create_stealth_context = _stealth_factories()
    state = load_storage_state()

    async with async_playwright() as p:
        browser = await create_browser(p, headless=True)
        ctx = await create_stealth_context(browser)
        try:
            await ctx.add_cookies(state["cookies"])
            yield await ctx.new_page()
        finally:
            await ctx.close()
            await browser.close()


async def with_session(work):
    """Run `work(page)` against an authenticated page, re-logging in once.

    The stored session lasts ~7 days, so expiry is routine rather than
    exceptional. With credentials configured an expired session is refreshed
    and the work retried; without them the AuthError propagates so the caller
    reports something actionable.
    """
    from expertflyer_login import credentials_available, login_and_save

    await ensure_state()
    try:
        async with session_page() as page:
            return await work(page)
    except AuthError:
        if not credentials_available():
            raise
        await login_and_save(os.environ[STATE_ENV])
        async with session_page() as page:
            return await work(page)


# Seat maps, availability and alerts arrive in RSC responses fetched after
# hydration, so they are not in the page's inlined __next_f payload. Listening
# for responses and re-reading their bodies is unreliable — Playwright cannot
# replay a streamed body, which made the seat map intermittently unreadable.
# Fetching each response through a route handler keeps the body in hand.
_COLLECTED_TYPES = ("document", "fetch", "xhr")


async def goto_collecting(page, url: str, settle_ms: int = 4000) -> list[str]:
    """Navigate and return the bodies of the data responses the page fetched."""
    bodies: list[str] = []

    async def handler(route):
        request = route.request
        if request.resource_type not in _COLLECTED_TYPES:
            await route.continue_()
            return
        try:
            response = await route.fetch()
            body = await response.text()
        except Exception:  # noqa: BLE001 - a request we cannot fetch is simply not a source
            await route.continue_()
            return
        bodies.append(body)
        await route.fulfill(response=response, body=body)

    await page.route("**/*", handler)
    try:
        await goto_checked(page, url, settle_ms=settle_ms)
    finally:
        await page.unroute("**/*", handler)
    return bodies


def first_payload_with(bodies, extract):
    """Apply `extract` to each body, returning the first truthy result."""
    for body in bodies:
        try:
            found = extract(body)
        except (KeyError, ValueError):
            continue
        if found:
            return found
    return None


async def goto_checked(page, url: str, settle_ms: int = 2500):
    """Navigate and convert the two failure shapes into typed errors."""
    resp = await page.goto(url, wait_until="networkidle", timeout=90000)
    status = resp.status if resp else None
    if AUTH_HOST in page.url:
        raise AuthError(
            "ExpertFlyer session expired (redirected to Auth0) — recapture the "
            f"storage_state at {os.environ.get(STATE_ENV, '<unset>')}."
        )
    if status == 403:
        raise BlockedError(
            "ExpertFlyer returned 403 with a valid session — the bot wall "
            "rejected this request. Do not retry in a loop; report it."
        )
    await page.wait_for_timeout(settle_ms)
    return resp


def emit(payload: dict) -> int:
    """Print one JSON object to stdout. Returns the process exit code."""
    print(json.dumps(payload))
    return 0


def fail(exc: Exception) -> int:
    """Report a typed failure on stdout as JSON plus a stderr diagnostic."""
    kind = getattr(exc, "kind", "error")
    print(json.dumps({"error": kind, "detail": str(exc)}))
    print(f"expertflyer: {exc}", file=sys.stderr)
    return 1
