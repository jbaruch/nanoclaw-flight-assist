"""The declared precheck budget and the timeouts nested inside it are one
ordered chain, and every link has to stay in order.

Three bounds sit inside each other on this path:

  byAir per-call  <  sync_tripit.py subprocess  <  declared precheck kill

`precheck_timeout_ms` in SKILL.md is what the agent-runner arms its kill
timer with. `_SYNC_SUBPROCESS_TIMEOUT` is what `main()` gives the
`sync_tripit.py` delegation, sized so the `subprocess.TimeoutExpired`
handler still has room to emit its safe-shape payload before that kill.
`_BYAIR_CALL_TIMEOUT_SECONDS` is what the byAir client gets inside the
child, sized so one hung upstream fails fast into the transient-transport
branch instead of eating the whole subprocess budget.

Invert any pair and the failure is silent rather than loud: the outer kill
lands first, the handler never runs, and a hung sync surfaces as
`execfile-error` with no payload. That is exactly the #212 shape — before
jbaruch/nanoclaw#890 the agent-runner applied a flat 30s to every precheck
and this file's 60s subprocess timeout sat behind a wall half its size, so
the handler was dead code for the life of the skill. #890 made the budget
per-skill, which is what makes the chain expressible at all — and what
lets it drift.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_precheck_path = REPO_ROOT / "skills" / "sync-tripit" / "precheck.py"
_spec = importlib.util.spec_from_file_location("sync_tripit_precheck_budget", _precheck_path)
assert _spec is not None and _spec.loader is not None, f"failed to locate {_precheck_path}"
precheck = importlib.util.module_from_spec(_spec)
sys.modules["sync_tripit_precheck_budget"] = precheck
_spec.loader.exec_module(precheck)

sys.path.insert(0, str(REPO_ROOT / "skills" / "flight-assist"))
import sync_tripit  # noqa: E402

SKILL_MD = REPO_ROOT / "skills" / "sync-tripit" / "SKILL.md"


def _declared_precheck_timeout_ms() -> int:
    """Read `precheck_timeout_ms` out of SKILL.md's YAML frontmatter.

    Deliberately narrow, matching the shape the agent-runner's own
    frontmatter reader accepts (`readFrontmatterScalar` in
    jbaruch/nanoclaw `container/agent-runner/src/skill-frontmatter.ts`):
    a bare integer scalar on its own line inside the leading `---` block,
    a leading BOM tolerated, CRLF accepted.

    Anchored with `re.match`, NOT `re.search` + `re.M`. The runtime reader
    only accepts a block at offset 0, so a search that can latch onto a
    later `---` (a markdown horizontal rule further down the body) would
    let this test pass while the real reader sees no declaration at all —
    the guard silently guarding nothing.
    """
    text = SKILL_MD.read_text(encoding="utf-8").lstrip("﻿")
    match = re.match(r"---\r?\n(.*?)^---[ \t]*\r?$", text, re.S | re.M)
    assert match, (
        f"{SKILL_MD} has no leading YAML frontmatter block — the agent-runner "
        "reads frontmatter only at offset 0, so nothing here is declared"
    )
    frontmatter = match.group(1)
    declared = re.search(r"^precheck_timeout_ms:\s*(\d+)\s*$", frontmatter, re.M)
    assert declared, (
        "sync-tripit must declare precheck_timeout_ms — its cadence is 5 "
        "minutes, the same as the default container kill, so an undeclared "
        "precheck wedges right up to the moment the next fire is due and is "
        "still holding the maintenance slot when it arrives"
    )
    return int(declared.group(1))


def test_declared_budget_matches_script_constant() -> None:
    declared_ms = _declared_precheck_timeout_ms()
    assert declared_ms == precheck._SCRIPT_KILL_BUDGET_SECONDS * 1000, (
        f"SKILL.md declares precheck_timeout_ms={declared_ms} but precheck.py "
        f"derives its subprocess budget from "
        f"_SCRIPT_KILL_BUDGET_SECONDS={precheck._SCRIPT_KILL_BUDGET_SECONDS}s. "
        "These are one number in two places — move both together."
    )


def test_subprocess_budget_leaves_room_for_the_timeout_handler() -> None:
    """The delegation must time out early enough that the handler's own
    stderr note and safe-shape payload still land before the kill."""
    assert precheck._SYNC_SUBPROCESS_TIMEOUT > 0, (
        "subprocess budget collapsed to zero or negative — every delegation "
        "would time out instantly and the skill would never sync"
    )
    assert precheck._SYNC_SUBPROCESS_TIMEOUT < precheck._SCRIPT_KILL_BUDGET_SECONDS, (
        f"the delegation budget ({precheck._SYNC_SUBPROCESS_TIMEOUT}s) reaches "
        f"the declared kill ({precheck._SCRIPT_KILL_BUDGET_SECONDS}s): the "
        "agent-runner kills the group before TimeoutExpired can be handled, "
        "and a hung sync surfaces as execfile-error with no payload (#212)"
    )


def test_byair_call_bound_fits_inside_the_subprocess_budget() -> None:
    """A single byAir call must not be able to consume the whole
    delegation budget — otherwise the graceful transient-transport branch
    is unreachable and every hung upstream costs a blunt subprocess kill.
    """
    assert sync_tripit._BYAIR_CALL_TIMEOUT_SECONDS < precheck._SYNC_SUBPROCESS_TIMEOUT, (
        f"byAir per-call timeout ({sync_tripit._BYAIR_CALL_TIMEOUT_SECONDS}s) "
        f"reaches the subprocess budget ({precheck._SYNC_SUBPROCESS_TIMEOUT}s): "
        "a single hung call rides the outer timeout instead of failing fast "
        "into the URLError branch, losing the diff (#212, the #28 collision)"
    )
