#!/usr/bin/env python3
"""Drive-engine precheck — the LIVE unified reconcile (airport + meeting drives).

The one engine that manages every `Drive:` block. On each ~30-min sweep it:

1. builds the airport drive legs from the byAir ∪ TripIt itinerary (R2 union, so a
   flight tracked by either source survives; storms suppressed, connections
   handled, origins resolved at the right instant), suppressing a trivial leg only
   when its boarding block exists on the byAir calendar (V3);
2. builds the meeting drive legs from the calendar (drive-planner's proven scan),
   masking flight events out by IDENTITY only (R5 — a ground meeting overlapping a
   redeye survives), with travel-away suppression so a home drive is never invented
   while abroad;
3. diffs both against the calendar's current blocks in ONE reconcile; and
4. APPLIES the plan — creating, updating, and deleting its own blocks — unless
   `DRIVE_ENGINE_SHADOW` is set, in which case it renders the plan to stderr and
   writes nothing (#183; the dry run that de-risks a block-shape cutover).

It does NOT touch legacy drive-planner / flight-assist blocks (`managed_legacy` is
empty): those are left for the operator to clean up, and the two old engines are
retired so they stop writing. The engine's own blocks carry the unified codec.

`build_plan` is the testable core (injected resolvers, no I/O). `main` wires the
real clients and is the OUTER PROCESS BOUNDARY — it fails CLOSED to a no-wake
payload on any error so a transient outage skips one sweep.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

_BUNDLE_DIR = Path(__file__).resolve().parent
if str(_BUNDLE_DIR) not in sys.path:
    sys.path.insert(0, str(_BUNDLE_DIR))

from airport_facts_cache import StaticAirport, load_static_facts, store_static_facts  # noqa: E402
from block_codec import ParsedBlock, parse_block  # noqa: E402
from calendar_apply import apply_plan  # noqa: E402
from engine import AirportInfo, build_reconcile_plan  # noqa: E402
from flight_mask import flight_codes, is_flight_event, known_flight_codes  # noqa: E402
from meeting_source import exclude_drive_block_events, meeting_desired_blocks  # noqa: E402
from normalize import flight_from_byair  # noqa: E402
from reconcile import DesiredBlock, ReconcilePlan  # noqa: E402
from shadow import plan_counts, render_plan  # noqa: E402
from tripit_flights import flights_from_schedule  # noqa: E402

RouteFn = Callable[[str, str], "timedelta | None"]

# The engine owns ONLY its own (unified) blocks for now; it never converges or
# deletes legacy drive-planner / flight-assist blocks (the operator cleans those
# up). Empty = touch no legacy generation.
_MANAGED_LEGACY: frozenset[str] = frozenset()

SWEEP_WINDOW = timedelta(days=14)

# Shadow / dry-run opt-in (#156 R4): plan and render, write nothing. Lets a
# block-shape change be validated against the production calendar before the
# cutover applies anything. The rendering itself lives in `shadow.py`.
_SHADOW_ENV = "DRIVE_ENGINE_SHADOW"
_SHADOW_TRUTHY = frozenset({"1", "true", "yes", "on"})

# Wall-clock budget for the APPLY (write) phase only. There is no plan-phase
# deadline: `reconcile_sweep` is a deterministic script with no LLM to bound, so
# a valid, fully-computed plan is never abandoned (#211 — the old 15s plan budget
# raised `PlanBudgetExceeded` on a good plan and froze the calendar for days).
# The plan phase runs to completion, bounded only by the per-call network
# timeouts below.
#
# The write phase still needs a bound: the host kills this precheck at ~30s
# (#164, SCRIPT_TIMEOUT_MS in the agent-runner), so `apply_plan` stops STARTING
# new ops once this budget is spent, returns a clean payload, and defers the rest
# to the next (idempotent) sweep. This is a FIXED budget, decoupled from how long
# the plan phase took — the old `budget - plan_elapsed` formula starved apply to
# 0 whenever planning ran long (#211). A cold sweep (many first-seen airports)
# can push plan_elapsed + this past the current 30s host kill; the mid-apply kill
# is idempotent-safe (deferred ops drain next sweep) and jbaruch/nanoclaw#890
# tracks a per-skill precheck-timeout override for real headroom.
_APPLY_PHASE_BUDGET_SECONDS = 20.0

# Per-call timeout for the sweep's own maps client (the shared default is 10s).
# The real watchdog against a hung provider now that there is no plan deadline: a
# single `travel_time` — worst case one Google call plus three sequential TomTom
# fallback calls — is bounded to ≤ 4 × 4s = 16s. A leg that times out is skipped
# this cycle and retried next sweep (the reconcile is idempotent).
_SWEEP_MAPS_TIMEOUT_SECONDS = 4.0

# Per-call timeout for the sweep's byAir client (the shared default is 30s). The
# watchdog against a hung `get_airport`: on a cache miss the sweep resolves each
# first-seen airport over the network, so an unbounded call could run to the host
# kill. A timed-out airport resolves to None → that flight is skipped this cycle
# and retried next sweep (idempotent). Warm sweeps hit the persisted static-facts
# cache and make no byAir call at all (#211).
_SWEEP_BYAIR_TIMEOUT_SECONDS = 6.0

# How close to departure a flight must be for the sweep to refresh its departure
# airport's live `delay.index` congestion nudge from byAir. Outside this window
# the nudge is noise (byAir's delay index is a near-term signal) and the airport
# resolves from the static-facts cache with no network call; inside it, the sweep
# pays one `get_airport` to keep the departure-clearance buffer honest (#211).
_DELAY_FRESHNESS_WINDOW = timedelta(hours=24)


def make_route(
    maps,
    *,
    cache: dict[tuple[str, str], timedelta | None] | None = None,
) -> RouteFn:
    """A memoizing `route(origin, destination) -> timedelta | None`.

    Per `MapsClient`'s own contract ("cache aggressively at the caller level"),
    dedupes identical (origin, destination) pairs within one sweep — an airport
    that is both a departure destination and a transfer origin is routed once, not
    per leg. Traffic is stable across a single sweep, so a cached duration is the
    same answer the provider would return again (#172). A failed route caches None
    too, so a dead endpoint isn't re-attempted every leg.

    There is no routing deadline: the plan phase runs to completion (#211).
    Runaway routing is bounded instead by the maps client's per-call timeout
    (`_SWEEP_MAPS_TIMEOUT_SECONDS`) — a hung provider fails the single leg (cached
    as None) rather than stalling the sweep.
    """
    import urllib.error

    from maps_client import MapsError

    memo: dict[tuple[str, str], timedelta | None] = {} if cache is None else cache

    def route(origin: str, destination: str) -> timedelta | None:
        key = (origin, destination)
        if key in memo:
            return memo[key]
        try:
            tt = maps.travel_time(origin, destination)
        except (MapsError, urllib.error.URLError, TimeoutError):
            memo[key] = None
            return None
        seconds = (
            tt.in_traffic_seconds if tt.in_traffic_seconds is not None else tt.duration_seconds
        )
        result = timedelta(seconds=seconds)
        memo[key] = result
        return result

    return route


@dataclass(frozen=True)
class ResolvedAirport:
    iata: str | None
    flag: str | None = None
    delay_index: str | None = None
    timezone: str | None = None


@dataclass(frozen=True)
class PlanResult:
    plan: ReconcilePlan
    skipped: tuple[str, ...] = field(default_factory=tuple)


def build_plan(
    *,
    flight_records: list[dict],
    resolve_airport: Callable[[int], ResolvedAirport | None],
    meeting_blocks: list[DesiredBlock],
    current_blocks: list[ParsedBlock],
    route: RouteFn,
    now: datetime,
    schedule: list[dict] | None = None,
    home_address: str | None = None,
    live_origin: str | None = None,
    tripit_flights: list | None = None,
    boarding_present: Callable | None = None,
) -> PlanResult:
    """Assemble the combined (airport + meeting) reconcile plan. Pure over inputs.

    Airport legs are built from the byAir records (airports resolved via
    `resolve_airport`) UNIONED with `tripit_flights` (already-normalized TripIt
    segments, R2) so a flight tracked by either source survives; the pre-built
    `meeting_blocks` are folded in as extra desired blocks so both diff against
    the calendar in one reconcile. `boarding_present` gates trivial-leg
    suppression (V3).
    """
    flights = list(tripit_flights or [])
    airport_info: dict[str, AirportInfo] = {}
    skipped: list[str] = []

    for record in flight_records:
        dep_id = record.get("dep_airport_id")
        arr_id = record.get("arr_airport_id")
        dep = resolve_airport(dep_id) if isinstance(dep_id, int) else None
        arr = resolve_airport(arr_id) if isinstance(arr_id, int) else None
        if dep is None or dep.iata is None or arr is None or arr.iata is None:
            skipped.append(f"flight {record.get('flight_id')}: unresolved airport(s)")
            continue
        try:
            flights.append(flight_from_byair(record, dep_iata=dep.iata, arr_iata=arr.iata))
        except ValueError as exc:
            skipped.append(str(exc))
            continue
        airport_info[dep.iata] = AirportInfo(
            flag=dep.flag, delay_index=dep.delay_index, timezone=dep.timezone
        )
        airport_info[arr.iata] = AirportInfo(
            flag=arr.flag, delay_index=arr.delay_index, timezone=arr.timezone
        )

    result = build_reconcile_plan(
        flights=flights,
        airport_info=airport_info,
        current_blocks=current_blocks,
        route=route,
        schedule=schedule,
        home_address=home_address,
        now=now,
        live_origin=live_origin,
        boarding_present=boarding_present,
        extra_desired=meeting_blocks,
        managed_legacy=_MANAGED_LEGACY,
    )
    return PlanResult(plan=result.plan, skipped=tuple(skipped) + result.skipped)


# --- real-client wiring (I/O) -----------------------------------------------


def _on_path(name: str) -> None:
    runtime = Path(f"/home/node/.claude/skills/tessl__{name}")
    target = runtime if runtime.is_dir() else _BUNDLE_DIR.parent / name
    if str(target) not in sys.path:
        sys.path.insert(0, str(target))


def _event_end(event: dict) -> datetime | None:
    end = event.get("end")
    raw = end.get("dateTime") if isinstance(end, dict) else None
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _boarding_block_end_times(calendar, find_events_args, items, byair_calendar_id, now):
    """End instants of boarding blocks on the byAir calendar, for trivial-leg
    suppression (V3 — the presence check lives on the byAir calendar, not primary).

    Returns [] when no byAir calendar is configured or the fetch fails — so a
    trivial leg is NOT suppressed without a confirmed boarding block (R6: never
    suppress the only 'head to the gate' signal silently)."""
    if not byair_calendar_id:
        return []
    import urllib.error

    from google_calendar_client import GoogleCalendarError

    try:
        raw = calendar.find_events(
            find_events_args(
                calendar_id=byair_calendar_id,
                time_min=(now - timedelta(hours=6)).isoformat(),
                time_max=(now + timedelta(days=21)).isoformat(),
            )
        )
    except (GoogleCalendarError, urllib.error.URLError):
        return []
    ends: list[datetime] = []
    for event in items(raw):
        if not isinstance(event, dict):
            continue
        summary = event.get("summary")
        if isinstance(summary, str) and summary.strip().lower().startswith("boarding"):
            end = _event_end(event)
            if end is not None:
                ends.append(end)
    return ends


def _fresh_live_origin(now: datetime, max_age_minutes: int) -> str | None:
    _on_path("flight-assist")
    from state import read_current_location

    loc = read_current_location()
    if not loc:
        return None
    try:
        when = datetime.fromisoformat(str(loc.get("captured_at")).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        return None
    age = (now - when.astimezone(timezone.utc)).total_seconds() / 60
    if age < 0 or age > max_age_minutes:
        return None
    lat, lng = loc.get("latitude"), loc.get("longitude")
    if isinstance(lat, int | float) and isinstance(lng, int | float):
        return f"{lat},{lng}"
    return None


def _group_meeting_adds(legs: list[dict]) -> list[dict]:
    """One entry per meeting (by identity), using its EARLIEST leg anchor — the
    outbound leg's meeting-start time — so the operator sees "<meeting> at
    <start>". Ordered by that anchor so the enumerated skip list reads
    chronologically (index 1 is the soonest drive)."""
    by_identity: dict[str, dict] = {}
    for leg in legs:
        cur = by_identity.get(leg["identity"])
        if cur is None or leg["anchor"] < cur["anchor"]:
            by_identity[leg["identity"]] = leg
    ordered = sorted(by_identity.values(), key=lambda leg: leg["anchor"])
    return [{"meeting": leg["meeting"], "when": leg["when"]} for leg in ordered]


def _dedup_material(updates: list[dict]) -> list[dict]:
    """One alert per meeting (by identity), the LARGEST drive-time swing — so a
    meeting whose outbound and return both moved is announced once. Ordered by
    anchor for a chronological read."""
    by_identity: dict[str, dict] = {}
    for u in updates:
        cur = by_identity.get(u["identity"])
        if cur is None or u["minutes"] > cur["minutes"]:
            by_identity[u["identity"]] = u
    ordered = sorted(by_identity.values(), key=lambda u: u["anchor"])
    return [
        {
            "meeting": u["meeting"],
            "minutes": u["minutes"],
            "direction": u["direction"],
            "when": u["when"],
        }
        for u in ordered
    ]


def render_notification(
    material_updates: list[dict], added_meeting_drives: list[dict]
) -> str | None:
    """Render the operator notice deterministically from the sweep's structured
    material — the fixed one-liners the wake agent used to compose by hand (#187).

    Returns None when there is nothing to say (both lists empty), matching the
    sweep's own wake gate. Building the message here instead of in the Haiku wake
    removes the cross-wake escalation vector root-caused in #187: a resumed
    weak-model session can no longer treat its own earlier (hallucinated) alarms as
    fact and ratchet them, because the notice is fixed text the agent relays
    verbatim, not a prompt it reasons over.

    `material_updates` are the projected `{meeting, minutes, direction, when}`
    dicts and `added_meeting_drives` the projected `{meeting, when}` dicts — both
    already grouped and ordered by `build_sweep_payload`. `direction` is `sooner`
    (drive got longer, leave earlier) or `later` (shorter)."""
    lines: list[str] = []
    for u in material_updates:
        lines.append(
            f"Traffic: leave {u['minutes']} min {u['direction']} "
            f"for your {u['meeting']} at {u['when']}"
        )
    if len(added_meeting_drives) == 1:
        d = added_meeting_drives[0]
        lines.append(
            f"Added a drive for {d['meeting']} at {d['when']} — "
            "reply 'skip' if you're not driving to it."
        )
    elif added_meeting_drives:
        lines.append(
            "Added drives — reply 'skip 1', 'skip 2', or e.g. 'skip 1 and 2' "
            "for any you're not driving to:"
        )
        for i, d in enumerate(added_meeting_drives, start=1):
            lines.append(f"{i}. {d['meeting']} at {d['when']}")
    return "\n".join(lines) if lines else None


def build_sweep_payload(applied, skipped: list[str]) -> dict:
    """Assemble the sweep's stdout payload and wake decision from an ApplyResult.

    Wakes the agent ONLY when the operator has something to act on: a new MEETING
    drive (which they can skip) or a MATERIAL drive-time change (leave earlier /
    later). Removes, airport-drive adds, converts, and routine sub-threshold
    re-times all apply SILENTLY — no wake, no message (the noise this gating
    exists to kill). `applied` is a `calendar_apply.ApplyResult`.

    `data.message` is the deterministically rendered operator notice (#187): the
    wake agent sends it verbatim rather than composing one, so a resumed session
    cannot escalate. The key is always present; its value is the notice string iff
    the sweep wakes, and `None` on a silent sweep."""
    added = _group_meeting_adds(applied.added_meeting_legs)
    material = _dedup_material(applied.material_updates)
    return {
        "wake_agent": bool(added) or bool(material),
        "data": {
            "applied": {
                "created": applied.created,
                "updated": applied.updated,
                "deleted": applied.deleted,
                "converted": applied.converted,
            },
            "added_meeting_drives": added,
            "material_updates": material,
            "message": render_notification(material, added),
            "deferred": applied.deferred,
            "skipped": len(skipped),
            "errors": len(applied.errors),
        },
    }


def build_shadow_payload(plan: ReconcilePlan, skipped: list[str]) -> dict:
    """Assemble the stdout payload for a shadow sweep — planned, never applied.

    Never wakes: a dry run changed nothing, so there is nothing for the operator
    to act on. `data.shadow` marks the payload so a reader can't mistake a
    rendered plan for applied work, and `planned` carries `shadow.plan_counts`
    (the #156 R4 acceptance surface — "the delete-diff matches the counted
    garbage") rather than the `applied` counts a live sweep reports."""
    return {
        "wake_agent": False,
        "data": {
            "shadow": True,
            "planned": plan_counts(plan),
            "skipped": len(skipped),
        },
    }


def _shadow_mode() -> bool:
    """True when `DRIVE_ENGINE_SHADOW` asks the sweep to plan and write nothing.

    Off unless explicitly set to a truthy value, so the scheduled sweep applies
    as normal; an operator opts a single run in from the shell (#156 R4, #183).
    """
    return os.environ.get(_SHADOW_ENV, "").strip().lower() in _SHADOW_TRUTHY


def finish_sweep(
    plan: ReconcilePlan,
    skipped: list[str],
    *,
    calendar,
    apply: Callable = apply_plan,
) -> dict:
    """Shadow-render or apply the plan, and return the sweep's stdout payload.

    The shadow-vs-live decision and the write itself, split out of `_run_sweep`
    so the safety contract is unit-testable without the live I/O clients (#183,
    the same seam #172 cut for `build_sweep_payload`). `apply` is injected for
    that: a test asserts a shadow run never calls it.

    SHADOW: renders to STDERR — stdout carries the JSON payload the scheduler
    parses, so writing the diff there would corrupt the contract — and returns
    BEFORE `apply` is called, so a shadow run cannot touch the calendar.

    LIVE: gives apply a FIXED write-phase budget (`_APPLY_PHASE_BUDGET_SECONDS`),
    independent of how long the plan phase took, so the write phase bounds its own
    duration and defers the rest to the next idempotent sweep (#164). This bounds
    the WRITE phase, not the whole sweep: on a warm sweep the total stays under
    the host precheck kill, but a long cold plan plus this budget can still exceed
    it — the mid-apply kill is idempotent-safe (deferred ops drain next sweep) and
    jbaruch/nanoclaw#890 tracks real headroom. Decoupling from plan elapsed is the
    #211 fix — the old `budget - elapsed` starved apply to 0 when planning ran
    long."""
    if _shadow_mode():
        print(render_plan(plan), file=sys.stderr)
        for line in skipped:
            print(f"[drive-engine] skip: {line}", file=sys.stderr)
        return build_shadow_payload(plan, skipped)

    applied = apply(
        plan,
        calendar=calendar,
        calendar_id="primary",
        budget_seconds=_APPLY_PHASE_BUDGET_SECONDS,
    )

    for line in skipped:
        print(f"[drive-engine] skip: {line}", file=sys.stderr)
    for line in applied.errors:
        print(f"[drive-engine] error: {line}", file=sys.stderr)

    return build_sweep_payload(applied, skipped)


class _AirportCtx(Protocol):
    """The byAir airport facts `airport_context` yields — the shape
    `_resolve_one_airport` reads. A Protocol (not the concrete `AirportContext`)
    so the policy stays importable without the flight-assist path and testable
    with a plain stand-in. Read-only members so the frozen `AirportContext`
    dataclass satisfies it."""

    @property
    def code(self) -> str | None: ...
    @property
    def flag(self) -> str | None: ...
    @property
    def delay_index(self) -> str | None: ...
    @property
    def timezone(self) -> str | None: ...


def _resolve_one_airport(
    *,
    static: StaticAirport | None,
    want_delay: bool,
    fetch: Callable[[], _AirportCtx | None],
) -> tuple[ResolvedAirport | None, StaticAirport | None]:
    """Pure resolution policy for one airport (#211). Returns
    `(resolved, new_static)`:

    - `resolved` is the `ResolvedAirport`, or None when it can't be resolved this
      sweep (a cache miss whose byAir fetch also failed) — the flight is skipped
      and retried next sweep.
    - `new_static` is a `StaticAirport` the caller should persist when this call
      learned a fresh real IATA (differing from `static`), else None.

    A warm static hit that needs no delay is served with ZERO fetches. Otherwise
    `fetch()` performs the byAir round trip (returning None on failure); the live
    `delay.index` is carried only when `want_delay`, and never persisted (it is
    not static). A None-IATA context is a transient miss — never cached, or it
    would pin the flight unresolvable forever."""
    if static is not None and not want_delay:
        return (
            ResolvedAirport(
                iata=static.iata, flag=static.flag, delay_index=None, timezone=static.timezone
            ),
            None,
        )
    ctx = fetch()
    if ctx is None:
        if static is not None:
            return (
                ResolvedAirport(
                    iata=static.iata, flag=static.flag, delay_index=None, timezone=static.timezone
                ),
                None,
            )
        return (None, None)
    resolved = ResolvedAirport(
        iata=ctx.code,
        flag=ctx.flag,
        delay_index=ctx.delay_index if want_delay else None,
        timezone=ctx.timezone,
    )
    new_static: StaticAirport | None = None
    if ctx.code is not None:
        fresh = StaticAirport(iata=ctx.code, flag=ctx.flag, timezone=ctx.timezone)
        if static != fresh:
            new_static = fresh
    return resolved, new_static


def _make_airport_resolver(
    *,
    static_facts: dict[int, StaticAirport],
    near_term_dep_ids: set[int],
    fetch_ctx: Callable[[int], _AirportCtx | None],
) -> tuple[Callable[[int], ResolvedAirport | None], Callable[[], bool]]:
    """Build the sweep's memoizing airport resolver + a dirty-flag reader (#211).

    Returns `(resolve, dirty)`: `resolve(airport_id)` yields the `ResolvedAirport`
    (or None when unresolvable this sweep), and `dirty()` reports whether any call
    learned a fresh static fact worth persisting.

    The per-sweep memo is keyed by `airport_id` ALONE, which is sufficient:
    `want_delay` is a pure function of the key (`airport_id in near_term_dep_ids`,
    a set fixed for the whole sweep), so it is identical on every call for a given
    airport. A cache hit therefore always carries the correct delay treatment — a
    near-term departure's live nudge can never be skipped by a hit seeded from an
    earlier arrival-side resolution of the same airport, since that earlier call
    saw the same `want_delay`. `fetch_ctx` owns the network + exception handling
    so the policy (`_resolve_one_airport`) stays pure; a repeated (origin) airport
    costs one byAir round trip, not one per leg."""
    memo: dict[int, ResolvedAirport | None] = {}
    dirty = False

    def resolve(airport_id: int) -> ResolvedAirport | None:
        nonlocal dirty
        if airport_id in memo:
            return memo[airport_id]
        resolved, new_static = _resolve_one_airport(
            static=static_facts.get(airport_id),
            want_delay=airport_id in near_term_dep_ids,
            fetch=lambda: fetch_ctx(airport_id),
        )
        if new_static is not None:
            static_facts[airport_id] = new_static
            dirty = True
        memo[airport_id] = resolved
        return resolved

    return resolve, lambda: dirty


def _near_term_departure_airport_ids(records: list[dict], now: datetime) -> set[int]:
    """Departure airport ids of byAir flights leaving within
    `_DELAY_FRESHNESS_WINDOW` of `now`.

    Only these airports get a live `delay.index` refresh from byAir; every other
    airport resolves from the static-facts cache with no network call. A flight
    already departed, or one whose `scheduled_dep_time` is missing or unparseable,
    contributes nothing — a past flight needs no drive, so its congestion nudge is
    moot (#211)."""
    ids: set[int] = set()
    horizon = now + _DELAY_FRESHNESS_WINDOW
    for record in records:
        dep_id = record.get("dep_airport_id")
        raw = record.get("scheduled_dep_time")
        if not isinstance(dep_id, int) or not isinstance(raw, str):
            continue
        try:
            dep = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if dep.tzinfo is None:
            continue
        if now <= dep <= horizon:
            ids.add(dep_id)
    return ids


def _run_sweep() -> dict:
    """Run the live unified reconcile and return the stdout payload.

    Wires the real clients, plans, then hands off to `finish_sweep`, which
    APPLIES the plan — or, under `DRIVE_ENGINE_SHADOW`, renders it and writes
    nothing (#183). The write itself lives there, not here.

    The plan phase runs to completion — no deadline (#211). Any exception
    propagates to `main()`'s fail-closed boundary. Split out from `main()` so the
    outer-boundary contract (error payload vs. work payload) is unit-testable
    without the live I/O clients (#172)."""
    import urllib.error

    _on_path("flight-assist")
    _on_path("travel-core")

    from airport_drive_inputs import airport_context
    from byair_client import ByAirClient, ByAirError
    from calendar_reconcile import _find_events_args, _items
    from fetch_events import CalendarFetcher
    from google_calendar_client import GoogleCalendarClient
    from home_address import HomeAddressError, read_current_home
    from maps_client import MapsClient
    from scan import scan
    from skip_state import load_active_skips
    from state import (
        MAX_LIVE_ORIGIN_AGE_MINUTES,
        read_active_flights,
        read_config,
        read_flight_state,
    )
    from trip_origin import (
        flight_summaries,
        load_travel_schedule,
        resolve_anchor,
    )

    now = datetime.now(timezone.utc)
    config = read_config() or {}
    # Home resolution (#162): the flight-assist config may have no home_address
    # key (a fresh cutover never provisioned it), so fall back to the canonical
    # user_profile current_home — the same source the retired drive-planner read.
    # Without this the sweep is DOA: the meeting scan raises on an empty home and
    # takes the whole cycle down.
    home_config = config.get("home_address")
    home = home_config if isinstance(home_config, str) and home_config.strip() else None
    if home is None:
        try:
            home = read_current_home()
        except HomeAddressError:
            home = None  # neither source configured — degrade, see below
    schedule = load_travel_schedule()

    maps = MapsClient.from_env(timeout=_SWEEP_MAPS_TIMEOUT_SECONDS)
    # One memoizing route closure for the whole sweep — meeting legs and airport
    # legs share it, so a repeated (origin, destination) pair costs one provider
    # round trip, not one per leg. Bounded by the maps client's per-call timeout,
    # not a routing deadline (#211).
    route = make_route(maps)

    # --- flight sources: byAir records + TripIt segments (R2 union) ---
    records = [
        record for fid in read_active_flights() if (record := read_flight_state(fid)) is not None
    ]
    tripit_flights = flights_from_schedule(schedule)

    # Known flight designators from the whole itinerary (both sources) — the
    # identity mask (R5) that keeps flight events out of the meeting scan.
    known_codes = known_flight_codes(
        [r.get("code") for r in records] + [f.code for f in tripit_flights]
    )
    for summary in flight_summaries(schedule):
        known_codes |= flight_codes(summary)

    calendar = GoogleCalendarClient()

    # --- V3: boarding-block presence on the byAir calendar ---
    boarding_ends = _boarding_block_end_times(
        calendar, _find_events_args, _items, config.get("byair_calendar_id"), now
    )

    def boarding_present(flight) -> bool:
        # A boarding block ends at ~departure; match it to this flight by time.
        dep = flight.effective_dep
        return any(abs((be - dep).total_seconds()) < 1800 for be in boarding_ends)

    # --- meeting side: fetch calendar, mask flights by IDENTITY (R5), scan ---
    # scan() requires a non-empty home_address. If neither the config nor the
    # user_profile provided one (#162), SKIP the meeting side with a diagnostic
    # rather than letting scan raise and take the airport side down with it — the
    # whole cycle must not be DOA over a missing home.
    meeting_blocks: list[DesiredBlock] = []
    meeting_skipped: list[str] = []
    if home:
        fetcher = CalendarFetcher()
        events = exclude_drive_block_events(
            fetcher.fetch_window(time_min=now, time_max=now + SWEEP_WINDOW)
        )
        # Drop flight events by identity only (R5 — never by time overlap, so a
        # ground meeting overlapping a redeye window survives). scan then runs
        # with an EMPTY flight context, since masking already happened here.
        events = [
            e
            for e in events
            if not (isinstance(e, dict) and is_flight_event(e.get("summary"), known_codes))
        ]
        skips = load_active_skips(now)

        def anchor_for(at: datetime) -> tuple[str | None, str | None]:
            anchor = resolve_anchor(schedule, at=at, home_address=home)
            return anchor.address, anchor.detail

        meetings = scan(
            events,
            now=now,
            home_address=home,
            skip_state=skips,
            anchor_for=anchor_for,
            flight_windows=[],
            flight_summaries=[],
        )
        meeting_blocks, meeting_skipped = meeting_desired_blocks(meetings, route=route)
    else:
        meeting_skipped = [
            "meeting side skipped: no home_address (flight-assist config and "
            "user_profile current_home both empty) — see #162"
        ]

    # --- airport facts ---
    # Static facts (IATA / flag / IANA tz) are immutable, so a warm sweep serves
    # them from the persisted cross-sweep cache with no byAir call — this is what
    # cut the ~7.6s ByAir cost that froze the calendar (#211). A byAir round trip
    # happens only on a cache MISS (first-seen airport) or to refresh the live
    # `delay.index` of a near-term DEPARTURE airport, where the nudge still moves
    # the block. `delay.index` is never persisted (it is live).
    byair = ByAirClient.from_env(timeout=_SWEEP_BYAIR_TIMEOUT_SECONDS)
    static_facts = load_static_facts()
    near_term_dep_ids = _near_term_departure_airport_ids(records, now)

    def fetch_ctx(airport_id: int) -> _AirportCtx | None:
        # Network + exception handling lives here so the resolver policy stays
        # pure and testable. A byAir failure (timeout / not_found) resolves to
        # None → the policy falls back to cached static facts, or skips the
        # flight this sweep (retried next; the reconcile is idempotent).
        try:
            return airport_context(byair.get_airport(airport_id))
        except (ByAirError, urllib.error.URLError):
            return None

    resolve_airport, static_facts_dirty = _make_airport_resolver(
        static_facts=static_facts,
        near_term_dep_ids=near_term_dep_ids,
        fetch_ctx=fetch_ctx,
    )

    # --- current blocks ---
    raw = calendar.find_events(
        _find_events_args(
            calendar_id="primary",
            time_min=(now - timedelta(days=2)).isoformat(),
            time_max=(now + timedelta(days=21)).isoformat(),
        )
    )
    current_blocks = [b for b in (parse_block(e) for e in _items(raw)) if b is not None]

    live_origin = _fresh_live_origin(now, MAX_LIVE_ORIGIN_AGE_MINUTES)

    result = build_plan(
        flight_records=records,
        resolve_airport=resolve_airport,
        meeting_blocks=meeting_blocks,
        current_blocks=current_blocks,
        route=route,
        tripit_flights=tripit_flights,
        boarding_present=boarding_present,
        now=now,
        schedule=schedule,
        home_address=home,
        live_origin=live_origin,
    )

    skipped = list(result.skipped) + list(meeting_skipped)

    # Persist any newly-resolved static airport facts for the next warm sweep.
    if static_facts_dirty():
        store_static_facts(static_facts)

    return finish_sweep(
        result.plan,
        skipped,
        calendar=calendar,
    )


def main() -> int:
    """Run the live unified reconcile and APPLY it — or, under
    `DRIVE_ENGINE_SHADOW`, render the plan and write nothing (#183). Fails
    closed on any error.

    outer-boundary-process-contract:
      caller's silent-failure shape — the scheduler reads a non-zero exit OR
        malformed stdout as "don't wake this cycle";
      what this catch emits — a valid {"wake_agent": ...} payload on stdout
        (traceback on stderr) and exit 0;
      why propagation breaks the contract — an uncaught exception would exit
        non-zero / print no payload, silently disabling the sweep.
    """
    try:
        payload = _run_sweep()
    # outer-boundary-process-contract:
    #   caller's silent-failure shape — the scheduler reads a non-zero exit OR
    #     malformed stdout as "don't wake this cycle";
    #   what this catch emits — a valid {"wake_agent": false, ...} payload on
    #     stdout (traceback on stderr) and exit 0, so the sweep is skipped cleanly;
    #   why propagation breaks the contract — an uncaught exception would exit
    #     non-zero and print no payload, silently disabling the sweep.
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        print(f"drive-engine precheck failed, no wake: {exc}", file=sys.stderr)
        print(json.dumps({"wake_agent": False, "data": {"error": str(exc)}}))
        return 0
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
