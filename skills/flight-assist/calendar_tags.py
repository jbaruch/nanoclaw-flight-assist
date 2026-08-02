"""Read flight-assist's managed-event tags from `extendedProperties.private`.

flight-assist recognizes its own managed calendar events (boarding blocks it
created, byAir flight events it adopted) by a small tag map — `faFlightId`,
`faKind`, `faManaged`.

Where the tags live
-------------------
The tags are stamped into `extendedProperties.private`, the native Calendar
API's machine-only field (nanoclaw#638). `calendar_reconcile` WRITES them there
on create/adopt, and `decode_private_props` READS them from there — a single
source.

The original design stamped these into `extendedProperties.private`, but the
Composio v3 toolkit this plugin first shipped on exposed no writable
`extendedProperties`, so for a transition window the tags rode in a compact
`<!--fa:{...}-->` JSON comment appended to the human description. #193 moved the
writer to `extendedProperties.private` and #178 migrated the live data; #200
dropped the dormant description-comment reader once zero live events carried it.
`strip_tags` survives to scrub any stray legacy `<!--fa:-->` comment out of a
human description on write, so an adopted byAir event's own content stays
tag-free.

stdlib-only per `coding-policy: dependency-management`.
"""

from __future__ import annotations

import re

# Matches a legacy `<!--fa:{...}-->` tag comment so `strip_tags` can scrub it
# out of a human description on write. Non-greedy body so a description with
# later HTML comments doesn't swallow them.
_TAG_RE = re.compile(r"\s*<!--fa:(?P<json>\{.*?\})-->", re.DOTALL)

# The managed-tag keys under `extendedProperties.private`. Already
# `fa`-namespaced, so they are collision-safe in the shared private map. Kept in
# sync with `calendar_plan`'s `TAG_*` constants by a test (drift guard).
TAG_KEYS = frozenset({"faFlightId", "faKind", "faManaged"})


def strip_tags(description: object) -> str:
    """The human description with any legacy `<!--fa:...-->` tag comment removed."""
    if not isinstance(description, str):
        return ""
    return _TAG_RE.sub("", description).rstrip()


def _decode_extended_tags(event: dict) -> dict | None:
    """The managed tags from `extendedProperties.private`, or None when absent.

    Returns None unless the private map carries a COMPLETE managed-tag set (all
    of `TAG_KEYS` present as strings) — a partial or malformed map means "not
    flight-assist-managed", never a partial return. `None` also covers a map
    with no `fa*` tags at all (an event with only a neighbour tool's private
    keys); only the `fa*` tag keys are extracted, so a neighbour's keys are
    never returned.
    """
    ext = event.get("extendedProperties")
    if not isinstance(ext, dict):
        return None
    private = ext.get("private")
    if not isinstance(private, dict):
        return None
    tags = {k: v for k, v in private.items() if k in TAG_KEYS and isinstance(v, str)}
    return tags if TAG_KEYS <= tags.keys() else None


def decode_private_props(event: object) -> dict:
    """Read flight-assist's managed-tag `private_props` from `extendedProperties`.

    Reads only `extendedProperties.private`, accepted as a COMPLETE tag set.
    Returns `{}` for an untagged or malformed event — "not flight-assist-managed",
    never an error.
    """
    if isinstance(event, dict):
        return _decode_extended_tags(event) or {}
    return {}
