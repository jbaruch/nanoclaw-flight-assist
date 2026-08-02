"""Tests for the flight-assist managed-event tag reader (`calendar_tags.py`).

The tags live in `extendedProperties.private`; `decode_private_props` reads them
from there as a complete set. `strip_tags` survives to scrub any stray legacy
`<!--fa:-->` comment out of a human description on write. These tests pin the
single-source read the reconcile depends on and the description-sanitize the
writer depends on.

Synthetic fixtures only.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "skills" / "flight-assist"))

from calendar_tags import (  # noqa: E402
    TAG_KEYS,
    decode_private_props,
    strip_tags,
)

TAGS = {"faFlightId": "100", "faKind": "boarding", "faManaged": "created"}

# A raw human description carrying a leftover legacy tag comment — the shape a
# pre-flip event still on disk would present. The reader must IGNORE it now.
_LEGACY_DESC = 'Gate B12\n<!--fa:{"faFlightId":"100","faKind":"boarding","faManaged":"created"}-->'


def _ext_event(private: dict, *, description: str | None = None) -> dict:
    """A fetched event carrying tags in extendedProperties.private."""
    event = {"id": "e1", "extendedProperties": {"private": private}}
    if description is not None:
        event["description"] = description
    return event


# --- strip_tags: still used by the writer to sanitize human descriptions -----


def test_strip_tags_removes_legacy_comment():
    assert strip_tags(_LEGACY_DESC) == "Gate B12"


def test_strip_non_string_is_empty_string():
    assert strip_tags(None) == ""


# --- decode_private_props: single-source extendedProperties reader (#178/#200) -


def test_decode_private_props_reads_extended_properties():
    assert decode_private_props(_ext_event(dict(TAGS))) == TAGS


def test_decode_private_props_ignores_legacy_description_comment():
    # A complete ext tag set wins; a leftover `<!--fa:-->` comment carrying
    # DIFFERENT tags in the description is never read (the fallback is gone, #200).
    ext = {"faFlightId": "999", "faKind": "flight", "faManaged": "adopted"}
    event = _ext_event(ext, description=_LEGACY_DESC)
    assert decode_private_props(event) == ext


def test_decode_private_props_partial_extended_ignores_description():
    # A partial new-shape map (faManaged present but faFlightId missing) is "not
    # managed" — even with a leftover legacy description comment, there is no
    # fallback anymore (#200), so the result is `{}`.
    ext = {"faKind": "boarding", "faManaged": "created"}  # no faFlightId
    event = _ext_event(ext, description=_LEGACY_DESC)
    assert decode_private_props(event) == {}


def test_decode_private_props_incomplete_extended_is_empty():
    # Incomplete ext → "not managed", never a partial map.
    ext = {"faManaged": "created"}  # only the marker, nothing else
    assert decode_private_props(_ext_event(ext)) == {}


def test_decode_private_props_ignores_neighbour_private_keys():
    private = {**TAGS, "someOtherTool": "x"}
    assert decode_private_props(_ext_event(private)) == TAGS


def test_decode_private_props_untagged_is_empty():
    assert decode_private_props({"id": "e1", "description": "just a meeting"}) == {}
    assert decode_private_props({"id": "e1"}) == {}
    assert decode_private_props(None) == {}


def test_tag_keys_match_calendar_plan_constants():
    # Drift guard: calendar_tags owns the extendedProperties key names; they must
    # stay identical to calendar_plan's TAG_* the writer stamps.
    from calendar_plan import TAG_FLIGHT_ID, TAG_KIND, TAG_MANAGED

    assert TAG_KEYS == {TAG_FLIGHT_ID, TAG_KIND, TAG_MANAGED}
