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
means. `skills/drive-engine/home_address.py` raises on an absent `current_home`
(a guessed drive origin mis-times every leg), while `home_metro_names` yields an
empty frozenset (an absent home metro means "suppress nothing", which is the
behaviour that predates the key).

Reads are gated on the block's own `schema_version` per
`coding-policy: stateful-artifacts`: a non-owner reader accepts the versions it
knows and treats anything else as no usable prior state. `ACCEPTED_SCHEMA_VERSIONS`
carries the rollout pair — writer and readers ship through separate pipelines,
so both versions are live at once for the rollout window and dual-accept is what
keeps that window zero-skew. A block with no `schema_version` line is legacy
pre-versioned data, read as v1.

stdlib-only per `coding-policy: dependency-management` (Stdlib First).

Public API:
    from addresses import (
        home_metro_names, is_home_metro, is_supported_version, profile_path,
        read_values, schema_version, section,
    )

    section(text)                     # the `## Addresses` body, or None
    schema_version(block)             # the declared version verbatim, or None
    is_supported_version(declared)    # whether this reader may read the block
    read_values("home_metro")         # ("Nashville, TN",) — tolerant, never raises
    home_metro_names()                # frozenset of normalized home-metro labels
    is_home_metro("Nashville, TN", names)     # True
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

DEFAULT_PROFILE_PATH = "/workspace/trusted/user_profile.md"
PROFILE_PATH_ENV = "USER_PROFILE_PATH"

# The block key carrying its own shape version, and the versions this reader
# accepts. v1 is the Epic #59 shape (`current_home`, `home_airport`,
# `new_home_wip`); v2 adds `home_metro`. Both are accepted because the owner
# (`nanoclaw-trusted`) and this reader ship through separate pipelines, so
# production runs the pair for the rollout window.
SCHEMA_VERSION_KEY = "schema_version"
ACCEPTED_SCHEMA_VERSIONS = frozenset({1, 2})

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


def schema_version(block: str) -> str | None:
    """The block's declared `schema_version`, verbatim, or None when absent.

    Returned unparsed so a caller's diagnostic can quote what the block
    actually said, malformed stamps included.
    """
    values = values_in(block, SCHEMA_VERSION_KEY)
    return values[0] if values else None


def is_supported_version(declared: str | None) -> bool:
    """Whether a block stamped `declared` may be read by this reader.

    An absent stamp is legacy pre-versioned data, read as v1 — the field was
    introduced at v1, so no earlier shape exists. A stamp outside
    `ACCEPTED_SCHEMA_VERSIONS`, or one that is not an integer at all, is a
    shape this reader does not know: per `coding-policy: stateful-artifacts` it
    is the reader that is lagging, so the answer is no usable prior state and
    an updated reader, never a guess at the new shape.
    """
    if declared is None:
        return True
    try:
        return int(declared) in ACCEPTED_SCHEMA_VERSIONS
    except ValueError:
        return False


def read_values(key: str, *, path: Path | None = None) -> tuple[str, ...]:
    """Every value for `key` in the profile's Addresses block. Never raises.

    Returns an empty tuple when the profile is absent, unreadable, carries no
    `## Addresses` block, carries a `schema_version` this reader does not
    accept, or carries no such key. Callers that must not guess (drive-engine's
    drive origin) read the block themselves and raise; callers whose absent
    value means "feature off" use this.

    The unsupported-version path lands on the same empty tuple as an unset key,
    which is the safe direction `coding-policy: stateful-artifacts` requires of
    a no-prior-state fallback: reading no home metro checks every trip, where
    guessing at an unknown block shape could silence the check.

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
    declared = schema_version(block)
    if not is_supported_version(declared):
        print(
            f"addresses: `## Addresses` block in {target} is stamped "
            f"schema_version={declared!r}, outside this reader's accepted "
            f"{sorted(ACCEPTED_SCHEMA_VERSIONS)} — reading `{key}` as unset; "
            "upgrade the jbaruch/nanoclaw-travel plugin",
            file=sys.stderr,
        )
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
