"""Tests for the cross-sweep static airport-facts cache (`airport_facts_cache.py`).

Exercises the documented contract (per `coding-policy: stateful-artifacts` and
`skills/drive-engine/state-schema.md`): round-trip persistence, missing-file
tolerance, and the hint-not-authority degradation — a corrupt, non-object, or
future-versioned file resolves to an empty map (refetch) rather than raising,
the deliberate opposite of the skip store's fail-closed read. The state
directory is redirected at a tmp_path via `DRIVE_PLANNER_STATE_DIR`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "drive-engine"))

from airport_facts_cache import (  # noqa: E402
    AIRPORT_FACTS_SCHEMA_VERSION,
    StaticAirport,
    load_static_facts,
    store_static_facts,
)


@pytest.fixture
def facts_env(tmp_path, monkeypatch):
    """Point DRIVE_PLANNER_STATE_DIR at a tmp dir; yield the cache file path."""
    monkeypatch.setenv("DRIVE_PLANNER_STATE_DIR", str(tmp_path))
    return tmp_path / "airport-facts.json"


@pytest.mark.usefixtures("facts_env")
def test_missing_file_returns_empty():
    assert load_static_facts() == {}


@pytest.mark.usefixtures("facts_env")
def test_round_trips_static_facts():
    facts = {
        3: StaticAirport(iata="JFK", flag="🇺🇸", timezone="America/New_York"),
        4: StaticAirport(iata="BNA", flag="🇺🇸", timezone="America/Chicago"),
    }
    store_static_facts(facts)
    assert load_static_facts() == facts


@pytest.mark.usefixtures("facts_env")
def test_persists_none_flag_and_timezone():
    facts = {7: StaticAirport(iata="XXX", flag=None, timezone=None)}
    store_static_facts(facts)
    assert load_static_facts() == facts


def test_written_file_is_versioned_and_string_keyed(facts_env):
    store_static_facts({3: StaticAirport(iata="JFK", flag="🇺🇸", timezone="America/New_York")})
    payload = json.loads(facts_env.read_text(encoding="utf-8"))
    assert payload["schema_version"] == AIRPORT_FACTS_SCHEMA_VERSION
    assert payload["airports"] == {"3": {"iata": "JFK", "flag": "🇺🇸", "tz": "America/New_York"}}


def test_corrupt_file_degrades_to_empty(facts_env, capsys):
    facts_env.write_text("{not json", encoding="utf-8")
    assert load_static_facts() == {}
    assert "unreadable" in capsys.readouterr().err


def test_non_object_file_degrades_to_empty(facts_env, capsys):
    facts_env.write_text("[1, 2, 3]", encoding="utf-8")
    assert load_static_facts() == {}
    assert "not a JSON object" in capsys.readouterr().err


def test_future_version_degrades_to_empty_not_raises(facts_env, capsys):
    """A hint cache treats a newer schema as no-usable-prior-state (refetch),
    NOT the fail-closed raise the skip store uses — a stale-vs-fresh airport
    fact costs latency, never a wrong block."""
    facts_env.write_text(
        json.dumps(
            {
                "schema_version": AIRPORT_FACTS_SCHEMA_VERSION + 1,
                "airports": {"3": {"iata": "JFK", "flag": "🇺🇸", "tz": "America/New_York"}},
            }
        ),
        encoding="utf-8",
    )
    assert load_static_facts() == {}
    assert "schema_version" in capsys.readouterr().err


def test_airports_not_a_dict_degrades_with_diagnostic(facts_env, capsys):
    facts_env.write_text(
        json.dumps({"schema_version": AIRPORT_FACTS_SCHEMA_VERSION, "airports": []}),
        encoding="utf-8",
    )
    assert load_static_facts() == {}
    assert "`airports` is not a JSON object" in capsys.readouterr().err


def test_malformed_entries_dropped_wellformed_kept(facts_env):
    facts_env.write_text(
        json.dumps(
            {
                "schema_version": AIRPORT_FACTS_SCHEMA_VERSION,
                "airports": {
                    "3": {"iata": "JFK", "flag": "🇺🇸", "tz": "America/New_York"},
                    "4": {"flag": "🇺🇸"},  # no iata — dropped
                    "5": "not-an-object",  # dropped
                    "notanint": {"iata": "AAA"},  # bad key — dropped
                },
            }
        ),
        encoding="utf-8",
    )
    assert load_static_facts() == {
        3: StaticAirport(iata="JFK", flag="🇺🇸", timezone="America/New_York")
    }


@pytest.mark.usefixtures("facts_env")
def test_load_normalizes_padded_and_empty_strings(facts_env):
    """A hand-edited / partly-corrupt cache is normalized on read (strip; empty →
    None), matching `airport_context._as_str`, so `'JFK '` / `''` never reach
    routing or block creation."""
    facts_env.write_text(
        json.dumps(
            {
                "schema_version": AIRPORT_FACTS_SCHEMA_VERSION,
                "airports": {
                    "3": {"iata": " JFK ", "flag": "", "tz": "  America/New_York "},
                    "4": {"iata": "   ", "flag": "🇺🇸", "tz": "X"},  # blank iata → dropped
                },
            }
        ),
        encoding="utf-8",
    )
    assert load_static_facts() == {
        3: StaticAirport(iata="JFK", flag=None, timezone="America/New_York")
    }


@pytest.mark.usefixtures("facts_env")
def test_store_overwrites_prior_cache():
    store_static_facts({3: StaticAirport(iata="JFK")})
    store_static_facts({4: StaticAirport(iata="BNA")})
    assert load_static_facts() == {4: StaticAirport(iata="BNA")}


def test_store_leaves_no_tmp_file(facts_env):
    store_static_facts({3: StaticAirport(iata="JFK")})
    leftovers = list(facts_env.parent.glob(".airport-facts.json.*"))
    assert leftovers == []
