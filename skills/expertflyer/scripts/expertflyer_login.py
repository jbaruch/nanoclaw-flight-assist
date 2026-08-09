"""Log in to ExpertFlyer and persist the session.

ExpertFlyer's session lasts ~7 days, so a captured storage_state alone means a
weekly manual re-capture — and it always expires while the operator is
travelling, which is exactly when they need it. With credentials present the
skill re-authenticates itself.

Auth is Auth0's hosted login (OIDC + PKCE) at auth.expertflyer.com. The
password posts in the request body, which is why OneCLI's gateway cannot carry
this credential: its generic-secret injection covers header, query param and
URL path only.

Set EXPERTFLYER_EMAIL and EXPERTFLYER_PASSWORD to enable automatic login.
"""

from __future__ import annotations

import os
from pathlib import Path

AUTH_HOST = "auth.expertflyer.com"
APP_HOST = "www.expertflyer.com"
EMAIL_ENV = "EXPERTFLYER_EMAIL"
PASSWORD_ENV = "EXPERTFLYER_PASSWORD"

# Any authenticated page will do; it bounces to Auth0 when signed out.
LOGIN_ENTRY = f"https://{APP_HOST}/alerts"

EMAIL_SELECTOR = "input[name=email]"
PASSWORD_SELECTOR = "input[name=password]"
SUBMIT_SELECTOR = "button[type=submit][name=submit]"

LOGIN_TIMEOUT_MS = 60000


def credentials_available() -> bool:
    return bool(os.environ.get(EMAIL_ENV) and os.environ.get(PASSWORD_ENV))


def _auth_error(message: str):
    """Login failures are auth failures, so callers report them as such."""
    from expertflyer_session import AuthError

    return AuthError(message)


def _credentials() -> tuple[str, str]:
    email = os.environ.get(EMAIL_ENV)
    password = os.environ.get(PASSWORD_ENV)
    if not (email and password):
        raise _auth_error(
            f"{EMAIL_ENV} and {PASSWORD_ENV} must both be set to log in "
            "automatically; otherwise supply a captured storage_state."
        )
    return email, password


async def login_and_save(state_path: str | Path) -> dict:
    """Authenticate and write a fresh storage_state. Never logs the password."""
    # Imported here so a caller without the stealth layer fails on its own
    # actionable message rather than on this module's import.
    from expertflyer_session import _stealth_factories  # noqa: PLC0415
    from playwright.async_api import async_playwright

    email, password = _credentials()
    create_browser, create_stealth_context = _stealth_factories()
    path = Path(state_path)

    async with async_playwright() as p:
        browser = await create_browser(p, headless=True)
        ctx = await create_stealth_context(browser)
        try:
            page = await ctx.new_page()
            await page.goto(LOGIN_ENTRY, wait_until="networkidle", timeout=LOGIN_TIMEOUT_MS)
            if AUTH_HOST not in page.url:
                # Already signed in on this fresh context: nothing to do beyond
                # persisting whatever session the redirect handed us.
                await ctx.storage_state(path=str(path))
                return {"logged_in": True, "already_authenticated": True, "state_path": str(path)}

            await page.wait_for_selector(EMAIL_SELECTOR, timeout=LOGIN_TIMEOUT_MS)
            await page.fill(EMAIL_SELECTOR, email)
            await page.fill(PASSWORD_SELECTOR, password)
            await page.click(SUBMIT_SELECTOR)

            try:
                await page.wait_for_url(lambda url: AUTH_HOST not in url, timeout=LOGIN_TIMEOUT_MS)
            except Exception as exc:  # noqa: BLE001 - reported as a login failure below
                message = await _auth_error_text(page)
                raise _auth_error(f"ExpertFlyer login did not complete: {message or exc}") from exc

            await page.wait_for_load_state("networkidle", timeout=LOGIN_TIMEOUT_MS)
            cookies = await ctx.cookies()
            session_cookies = [
                name
                for name in (c.get("name", "") for c in cookies)
                if name.startswith("__session")
            ]
            if not session_cookies:
                raise _auth_error(
                    "login returned to the app but no __session cookie was set — "
                    "treat as NOT logged in"
                )
            await ctx.storage_state(path=str(path))
            return {
                "logged_in": True,
                "already_authenticated": False,
                "session_cookies": session_cookies,
                "state_path": str(path),
            }
        finally:
            await ctx.close()
            await browser.close()


async def _auth_error_text(page) -> str:
    """The message Auth0 rendered, so a bad password says so."""
    for selector in (".auth0-global-message-error", "[role=alert]", ".error-message"):
        node = page.locator(selector).first
        if await node.count():
            text = (await node.inner_text()).strip()
            if text:
                return text
    return ""
