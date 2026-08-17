"""Read the operator's canonical home address — the drive origin.

Every home-anchored drive leg (outbound from home, return to home) starts or
ends at the operator's current residence. That address has ONE canonical home
(Epic #59 §4): the machine-readable `## Addresses` block in the owner profile
`/workspace/trusted/user_profile.md`, owned by the `trusted-memory` skill in
the `nanoclaw-trusted` plugin. drive-engine is a READER of that block, never a
writer — the trusted plugin owns its shape and migration. (Epic #59 §4/§7 name
`nanoclaw-admin`; that is stale — the owning `trusted-memory` skill lives in
`nanoclaw-trusted` (whose `state-schema.md` names this reader), and this
reader itself lives in the `jbaruch/nanoclaw-travel` plugin.)

Read by `reconcile_sweep.py`, which resolves the home origin for every
home-anchored leg it plans.

The block the trusted plugin writes (Epic #59 §4):

    ## Addresses
    <!-- canonical, machine-read by travel tile -->
    - current_home: 12 Example St, Sampleton, TN 37000
    - home_airport: BNA
    - new_home_wip: 99 Placeholder Rd, Testburg, TN 37100

`current_home` is the drive origin. `new_home_wip` (a house under
construction) is deliberately NOT read — switching origins is a later,
explicit change, not an automatic pickup of whichever address appears first.

The block parse itself lives in `travel-core/addresses.py`, which every bundle
reading this block shares; this module owns what an absent `current_home`
means for drive planning.

This is the deterministic reader (per `coding-policy: script-delegation` — a
fixed parse of a fixed block). It does NOT fall back to a guessed address: a
silent wrong origin would route every drive from the wrong place and quietly
mis-time every leave-by. A missing block raises with an actionable message
pointing at the trusted plugin.

stdlib-only per `coding-policy: dependency-management` (Stdlib First).

Public API:
    from home_address import read_current_home, HomeAddressError

    home = read_current_home()   # "12 Example St, Sampleton, TN 37000"
"""

from __future__ import annotations

import sys
from pathlib import Path

_BUNDLE_DIR = Path(__file__).resolve().parent
_TRAVEL_CORE = Path("/home/node/.claude/skills/tessl__travel-core")
if not _TRAVEL_CORE.is_dir():
    _TRAVEL_CORE = _BUNDLE_DIR.parent / "travel-core"
if str(_TRAVEL_CORE) not in sys.path:
    sys.path.insert(0, str(_TRAVEL_CORE))

from addresses import profile_path as _addresses_profile_path  # noqa: E402
from addresses import section as _addresses_section  # noqa: E402
from addresses import values_in  # noqa: E402

# The canonical drive-origin key inside the `## Addresses` block.
_CURRENT_HOME_KEY = "current_home"


class HomeAddressError(Exception):
    """Raised when the canonical home address cannot be read.

    The fix is always "make the trusted plugin's `## Addresses` block present and
    well-formed", not "retry" — the message says so. drive-engine refuses to
    guess an origin rather than route every drive from the wrong place.
    """


def profile_path() -> Path:
    """The owner-profile path; overridable via `USER_PROFILE_PATH` for tests."""
    return _addresses_profile_path()


def read_current_home(*, path: Path | None = None) -> str:
    """Return the `current_home` address from the canonical Addresses block.

    Args:
        path: override the profile path (defaults to `profile_path()`).

    Returns:
        The `current_home` value, whitespace-trimmed.

    Raises:
        HomeAddressError: when the profile file is missing, carries no
            `## Addresses` block, or that block carries no non-empty
            `current_home:` entry — each with a message pointing at the
            `nanoclaw-trusted` trusted-memory Addresses block to fix. A
            `current_home:` outside the block is deliberately not read.
    """
    target = path if path is not None else profile_path()
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise HomeAddressError(
            f"owner profile not found at {target} — the canonical home address lives in the "
            "`## Addresses` block of user_profile.md, owned by the nanoclaw-trusted trusted-memory "
            "skill; add the block (current_home: <address>) and redeploy"
        ) from exc
    except OSError as exc:
        raise HomeAddressError(f"owner profile at {target} is unreadable ({exc})") from exc

    section = _addresses_section(text)
    if section is None:
        raise HomeAddressError(
            f"no `## Addresses` block in {target} — the canonical home address lives in that "
            "block of user_profile.md (nanoclaw-trusted trusted-memory); add it with a "
            "`- current_home: <address>` line and redeploy"
        )
    values = values_in(section, _CURRENT_HOME_KEY)
    if not values:
        raise HomeAddressError(
            f"no `current_home:` entry in the `## Addresses` block of {target} — add "
            "`- current_home: <address>` to the canonical block (nanoclaw-trusted trusted-memory)"
        )
    # One residence, one origin: a block carrying two `current_home:` lines is
    # malformed on the owner's side, and the first is the one the owner's own
    # rewrite keeps.
    return values[0]
