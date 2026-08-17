"""Reader for the canonical `## Addresses` block in the owner profile.

The block lives in `/workspace/trusted/user_profile.md` and is owned by the
`trusted-memory` skill in the `nanoclaw-trusted` plugin (its `state-schema.md`
names this side as the travel-tile reader). Every travel bundle here is a
READ-ONLY, non-migrating consumer per `coding-policy: stateful-artifacts` —
the trusted plugin owns the shape, bumps its version, and migrates it.

The block the trusted plugin writes:

    ## Addresses
    <!-- canonical, machine-read by travel tile; schema v2 -->
    - schema_version: 2
    - current_home: 12 Example St, Sampleton, TN 37000
    - home_airport: BNA
    - home_metro: Nashville, TN
    - new_home_wip: 99 Placeholder Rd, Testburg, TN 37100

This module owns the parse alone; each consumer owns what a missing value
means. `drive-engine/home_address.py` raises on an absent `current_home` (a
guessed drive origin mis-times every leg), while `home_metro_names` returns an
empty tuple (an absent home metro means "suppress nothing", which is the
behaviour that predates the key).

The block's own `schema_version` is deliberately NOT gated on. Keys are read by
name, an unknown key is ignored, and an absent key reads as absent — so a
reader stays dual-accept across the trusted plugin's bumps, which is what
`coding-policy: stateful-artifacts` (Cross-Pipeline Schema Bumps) requires of
a reader shipping through a separate pipeline from its writer.

stdlib-only per `coding-policy: dependency-management` (Stdlib First).

Public API:
    from addresses import (
        home_metro_names, is_home_metro, profile_path, read_values, section,
    )

    section(text)                     # the `## Addresses` body, or None
    read_values("home_metro")         # ("Nashville, TN",) — tolerant, never raises
    home_metro_names()                # normalized home-metro labels
    is_home_metro("Nashville\\, TN", names)   # True
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

DEFAULT_PROFILE_PATH = "/workspace/trusted/user_profile.md"
PROFILE_PATH_ENV = "USER_PROFILE_PATH"

# The `## Addresses` section heading. Values are read ONLY from inside this
# canonical block — a `current_home:` or `home_metro:` mention elsewhere in the
# profile (prose, an example, a stale note) must never feed a travel decision.
_ADDRESSES_HEADING_RE = re.compile(r"^[ \t]*##[ \t]+Addresses[ \t]*$", re.MULTILINE)
# The next `## ` heading, which closes the Addresses section.
_NEXT_H2_RE = re.compile(r"^[ \t]*##[ \t]+\S", re.MULTILINE)

# Collapses runs of whitespace so `Nashville,  TN` and `Nashville, TN` compare
# equal. Casefolding and this collapse are the whole normalization: matching is
# exact-equality on the normalized label, never a substring or fuzzy test. A
# location string is `<City>, <Region>` from TripIt and a hand-typed metro from
# the profile, and "does this text mean that place?" is reasoning, not scripting
# (`coding-policy: script-delegation`, the Regex Trap).
_WHITESPACE_RUN_RE = re.compile(r"\s+")


def profile_path() -> Path:
    """The owner-profile path; overridable via `USER_PROFILE_PATH` for tests."""
    return Path(os.environ.get(PROFILE_PATH_ENV, DEFAULT_PROFILE_PATH))


def section(text: str) -> str | None:
    """The body of the `## Addresses` block, or None when the heading is absent.

    Runs from just after the `## Addresses` heading to the next `## ` heading
    (or end of file). Scoping every read to this block is what keeps a stale or
    example key elsewhere in the profile from silently feeding a travel
    decision.
    """
    heading = _ADDRESSES_HEADING_RE.search(text)
    if heading is None:
        return None
    body = text[heading.end() :]
    nxt = _NEXT_H2_RE.search(body)
    return body[: nxt.start()] if nxt else body


def values_in(block: str, key: str) -> tuple[str, ...]:
    """Every `- <key>: <value>` value in an Addresses block body, in file order.

    Repeated lines all count: the block carries one value per line, and a key
    that legitimately has several (a metro area spelled more than one way in
    the feed) says so by repeating rather than by packing a separator into a
    value that already contains commas.

    Blank values are dropped — a `- home_metro:` line with nothing after it is
    an unset key, not an empty-string match that would suppress every trip
    whose destination the feed left blank.
    """
    pattern = re.compile(
        rf"^\s*-\s*{re.escape(key)}\s*:\s*(?P<value>\S.*?)\s*$",
        re.MULTILINE,
    )
    return tuple(match["value"].strip() for match in pattern.finditer(block))


def read_values(key: str, *, path: Path | None = None) -> tuple[str, ...]:
    """Every value for `key` in the profile's Addresses block. Never raises.

    Returns an empty tuple when the profile is absent, unreadable, carries no
    `## Addresses` block, or carries no such key. Callers that must not guess
    (drive-engine's drive origin) read the block themselves and raise; callers
    whose absent value means "feature off" use this.

    A profile that is simply not mounted reads as absent and stays silent — not
    every tier mounts `/workspace/trusted`. A profile that IS there but cannot
    be read is a real failure and gets a stderr diagnostic, per
    `coding-policy: error-handling` (best-effort work that continues past a
    failure still says so).
    """
    target = path if path is not None else profile_path()
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ()
    except (OSError, UnicodeDecodeError) as exc:
        print(
            f"addresses: owner profile at {target} is unreadable "
            f"({type(exc).__name__}) — reading `{key}` as unset",
            file=sys.stderr,
        )
        return ()
    block = section(text)
    if block is None:
        return ()
    return values_in(block, key)


def normalize_location(value: str) -> str:
    """A location label reduced to its comparison form.

    Casefolded with whitespace runs collapsed, so `Nashville, TN` from a
    hand-typed profile line and `Nashville,  TN` off the feed compare equal.
    """
    return _WHITESPACE_RUN_RE.sub(" ", value).strip().casefold()


def home_metro_names(*, path: Path | None = None) -> frozenset[str]:
    """The operator's home-metro labels, normalized for comparison.

    Empty when the profile carries no `home_metro` key — the pre-key behaviour,
    where nothing is treated as home and every trip is checked for bookings.
    """
    return frozenset(
        normalized
        for value in read_values("home_metro", path=path)
        if (normalized := normalize_location(value))
    )


def is_home_metro(destination: str | None, names: frozenset[str]) -> bool:
    """Whether `destination` is one of the operator's home-metro labels.

    A blank or absent destination is never home: an unlabelled trip is a trip
    whose destination is unknown, and reading unknown as home would silence the
    booking check for exactly the trips it exists to watch.
    """
    if not names or not isinstance(destination, str):
        return False
    return normalize_location(destination) in names
