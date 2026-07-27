"""Cross-sweep cache of STATIC airport facts — IATA code, country flag, IANA tz.

Those three are immutable per airport, but the sweep re-fetched them from byAir
every ~30-min cycle: 13 airports × ~0.6s = ~7.6s re-paid each run, the dominant
cost that pushed the plan phase past its old budget and froze the calendar
(#211). This persists them so a warm sweep resolves a known airport with zero
network calls.

NOT cached here: byAir's live `delay.index` congestion nudge, which shifts
through the day. The sweep fetches that live and only for near-term departures,
where it actually moves the block (`reconcile_sweep.resolve_airport`).

Hint, not authority (per `coding-policy: stateful-artifacts`). A missing OR
unreadable OR future-versioned file simply means "resolve from byAir this
sweep" — the static facts re-derive trivially, so a bad cache degrades to the
pre-cache behaviour (one slow sweep) and never raises. That is the DELIBERATE
OPPOSITE of `skip_state.py`, whose empty fallback would resurrect declined
meetings as a nag: here an empty fallback costs latency only, never
correctness, so the safe move is to swallow-and-refetch, not fail closed.

State file (see `state-schema.md`):
    <state_dir>/airport-facts.json
    {"schema_version": 1, "airports": {"<airport_id>": {"iata","flag","tz"}}}

stdlib-only per `coding-policy: dependency-management` (Stdlib First).

Public API:
    from airport_facts_cache import StaticAirport, load_static_facts, store_static_facts

    facts = load_static_facts()               # {airport_id: StaticAirport}
    facts[3] = StaticAirport("JFK", "🇺🇸", "America/New_York")
    store_static_facts(facts)                 # atomic rewrite
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

# Co-locates with the skip store so all drive-engine state lives in one dir; the
# owner of the directory path is `skip_state.state_dir` (single source of truth,
# same `DRIVE_PLANNER_STATE_DIR` override tests point at a tmp_path).
from skip_state import state_dir

AIRPORT_FACTS_SCHEMA_VERSION = 1

_FACTS_FILE = "airport-facts.json"


@dataclass(frozen=True)
class StaticAirport:
    """The immutable byAir facts for one airport. `iata` is always present — a
    None-IATA resolution is a transient miss, never cached (it would pin a flight
    as unresolvable forever)."""

    iata: str
    flag: str | None = None
    timezone: str | None = None


def _facts_path() -> Path:
    return state_dir() / _FACTS_FILE


def load_static_facts() -> dict[int, StaticAirport]:
    """Return the persisted `{airport_id: StaticAirport}` map, or `{}` when there
    is no usable cache.

    A missing, unreadable, malformed, or future-versioned file all resolve to
    `{}` — the sweep then re-fetches from byAir, exactly the pre-cache behaviour.
    A diagnostic goes to stderr for anything other than a plain missing file, so
    a corrupt cache is visible without being fatal (fail visibly, don't fail the
    sweep). Malformed individual entries are dropped; a well-formed remainder is
    still returned.
    """
    path = _facts_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(
            f"[drive-engine] airport-facts cache {path} unreadable ({exc}); "
            "resolving airports from byAir this sweep",
            file=sys.stderr,
        )
        return {}
    if not isinstance(payload, dict):
        print(
            f"[drive-engine] airport-facts cache {path} is not a JSON object; refetching",
            file=sys.stderr,
        )
        return {}
    version = payload.get("schema_version")
    if version != AIRPORT_FACTS_SCHEMA_VERSION:
        # A future version is not an error for a pure hint cache — treat it as
        # "no usable prior state" and refetch (safe, non-disruptive: costs one
        # slow sweep, never a wrong block). A newer writer's file survives
        # untouched until this reader is upgraded (`stateful-artifacts`).
        #
        # The refetch is safe even if byAir is ALSO down: a cache-miss airport
        # that byAir can't resolve makes `reconcile_sweep._resolve_one_airport`
        # raise `AirportUnresolved`, failing the whole sweep closed rather than
        # building a partial plan that would orphan-delete live blocks (#211).
        print(
            f"[drive-engine] airport-facts cache {path} schema_version {version!r} "
            f"!= {AIRPORT_FACTS_SCHEMA_VERSION}; refetching this sweep",
            file=sys.stderr,
        )
        return {}
    airports = payload.get("airports")
    if not isinstance(airports, dict):
        print(
            f"[drive-engine] airport-facts cache {path} `airports` is not a JSON "
            "object; refetching",
            file=sys.stderr,
        )
        return {}
    facts: dict[int, StaticAirport] = {}
    for raw_id, entry in airports.items():
        try:
            airport_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if not isinstance(entry, dict):
            continue
        iata = entry.get("iata")
        if not isinstance(iata, str) or not iata:
            continue
        flag = entry.get("flag")
        tz = entry.get("tz")
        facts[airport_id] = StaticAirport(
            iata=iata,
            flag=flag if isinstance(flag, str) else None,
            timezone=tz if isinstance(tz, str) else None,
        )
    return facts


def store_static_facts(facts: dict[int, StaticAirport]) -> None:
    """Atomically persist the `{airport_id: StaticAirport}` map (temp file +
    rename, so a crash mid-write never strands a half-written cache).

    Entries are keyed by the airport id as a string (JSON object keys are
    strings). The write is best-effort from the caller's view — it is only a
    latency optimisation — but a failure to write still propagates so a genuinely
    broken state dir surfaces rather than silently disabling the cache forever.
    """
    payload = {
        "schema_version": AIRPORT_FACTS_SCHEMA_VERSION,
        "airports": {
            str(airport_id): {
                "iata": fact.iata,
                "flag": fact.flag,
                "tz": fact.timezone,
            }
            for airport_id, fact in sorted(facts.items())
        },
    }
    path = _facts_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
