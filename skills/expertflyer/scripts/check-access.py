#!/usr/bin/env python3
"""Diagnose ExpertFlyer access: is the session dead, or is it the bot wall?

Both surface as HTTP 403 to a naive caller, so the distinction is made by what
the page does: a redirect to auth.expertflyer.com means the ~7-day session
expired; a 403 with a valid session means the bot wall rejected the request.

Output: one JSON object on stdout. Exit non-zero when access is not healthy.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from expertflyer_session import (  # noqa: E402
    STATE_ENV,
    AuthError,
    BlockedError,
    ExpertFlyerError,
    emit,
    fail,
    goto_checked,
    load_storage_state,
    with_session,
)

PROBE_URL = "https://www.expertflyer.com/alerts"


async def run() -> int:
    async def work(page):
        await goto_checked(page, PROBE_URL, settle_ms=2500)
        return await page.inner_text("body")

    # Reach the site first: with_session logs in when no session file exists,
    # so reading the file up front would fail before login ever ran.
    body = await with_session(work)
    state = load_storage_state()
    cookie_names = sorted({c.get("name", "") for c in state.get("cookies", ())})
    return emit(
        {
            "ok": True,
            "storage_state": os.environ.get(STATE_ENV),
            "session_cookies": [n for n in cookie_names if n.startswith("__session")],
            "reached": PROBE_URL,
            "alerts_page_loaded": "active alerts remaining" in body,
        }
    )


def main() -> int:
    try:
        return asyncio.run(run())
    except (AuthError, BlockedError, ExpertFlyerError, ValueError) as exc:
        return fail(exc)


if __name__ == "__main__":
    sys.exit(main())
