"""The declared precheck budget and the script's internal constant are
one number in two places.

`precheck_timeout_ms` in SKILL.md is what the agent-runner arms its kill
timer with; `_SCRIPT_KILL_BUDGET_SECONDS` in precheck.py is what
`_run_cycle` sizes its poll loop against so a poll started just under
the budget still returns before that kill. If they drift, the loop
either overruns the kill (cycles die as `execfile-error`, the
jbaruch/nanoclaw#562 wake-storm shape) or stops polling far too early
and defers work it had time to do.

Before jbaruch/nanoclaw#890 the 30s was a flat global the agent-runner
applied to every precheck, so the script constant only had to match a
fleet-wide fact and nothing could desync it. #890 made the budget
per-skill and removed the global, which created the drift this test
closes.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "skills" / "flight-assist"))

import precheck  # noqa: E402

SKILL_MD = REPO_ROOT / "skills" / "flight-assist" / "SKILL.md"


def _declared_precheck_timeout_ms() -> int:
    """Read `precheck_timeout_ms` out of SKILL.md's YAML frontmatter.

    Deliberately narrow — a bare integer scalar on its own line inside
    the leading `---` block, matching the shape the agent-runner's own
    frontmatter reader accepts (`readFrontmatterScalar` in
    jbaruch/nanoclaw `container/agent-runner/src/skill-frontmatter.ts`).
    """
    text = SKILL_MD.read_text(encoding="utf-8")
    match = re.search(r"^---\n(.*?)^---\s*$", text, re.S | re.M)
    assert match, f"{SKILL_MD} has no leading YAML frontmatter block"
    frontmatter = match.group(1)
    declared = re.search(r"^precheck_timeout_ms:\s*(\d+)\s*$", frontmatter, re.M)
    assert declared, (
        "flight-assist must declare precheck_timeout_ms — it fires every 2 "
        "minutes, so an undeclared precheck runs to the container kill and "
        "would still hold the maintenance slot when the next fire is due"
    )
    return int(declared.group(1))


def test_declared_budget_matches_script_constant() -> None:
    declared_ms = _declared_precheck_timeout_ms()
    assert declared_ms == precheck._SCRIPT_KILL_BUDGET_SECONDS * 1000, (
        f"SKILL.md declares precheck_timeout_ms={declared_ms} but precheck.py "
        f"sizes its poll loop against "
        f"_SCRIPT_KILL_BUDGET_SECONDS={precheck._SCRIPT_KILL_BUDGET_SECONDS}s. "
        "These are one number in two places — move both together."
    )


def test_cycle_budget_leaves_room_for_one_in_flight_poll() -> None:
    """The loop must stop starting polls early enough that a poll begun
    at the last instant still finishes before the kill.

    The headroom covers one byAir poll plus one Maps travel-time query —
    both happen inside a single `_process_flight` — plus interpreter
    teardown. Asserting the relation rather than the literal keeps the
    check correct when either call timeout changes.
    """
    assert precheck._CYCLE_WALL_CLOCK_BUDGET_SECONDS > 0, (
        "cycle budget collapsed to zero or negative — the poll loop would "
        "defer every flight and the skill would never do work"
    )
    worst_case_in_flight = (
        precheck._CYCLE_WALL_CLOCK_BUDGET_SECONDS + precheck._CYCLE_POLL_HEADROOM_SECONDS
    )
    assert worst_case_in_flight <= precheck._SCRIPT_KILL_BUDGET_SECONDS, (
        "a poll started at the budget edge can outlive the kill: "
        f"{worst_case_in_flight}s > {precheck._SCRIPT_KILL_BUDGET_SECONDS}s"
    )
