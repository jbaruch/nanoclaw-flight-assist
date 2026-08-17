"""Tests for the shared `## Addresses` block reader (`travel-core/addresses.py`).

Builds the canonical block programmatically in a tmp file (no fixtures checked
in, per `coding-policy: testing-standards`) and pins the contract the booking
check leans on:

  - values are read ONLY from inside the `## Addresses` block
  - a repeated key yields every value, in file order; a blank value is unset
  - a tolerant read never raises — an absent profile, an absent block, and an
    absent key all read as "unset" (which means "suppress nothing")
  - an unreadable-but-present profile says so on stderr
  - `is_home_metro` matches on the normalized label alone: case-insensitive,
    whitespace-collapsed, never a substring test
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "travel-core"))

from addresses import (  # noqa: E402
    ACCEPTED_SCHEMA_VERSIONS,
    home_metro_names,
    is_home_metro,
    is_supported_version,
    normalize_location,
    read_values,
    schema_version,
    section,
    values_in,
)

CANONICAL_BLOCK = """\
# Owner Profile

Some prose about the operator.

## Addresses
<!-- canonical, machine-read by travel tile; schema v2 -->
- schema_version: 2
- current_home: 12 Example St, Sampleton, TN 37000
- home_airport: BNA
- home_metro: Nashville, TN
- new_home_wip: 99 Placeholder Rd, Testburg, TN 37100

## See also

- nothing here
"""


def _write_profile(tmp_path: Path, text: str) -> Path:
    profile = tmp_path / "user_profile.md"
    profile.write_text(text, encoding="utf-8")
    return profile


# --- block scoping ----------------------------------------------------------


def test_section_stops_at_the_next_heading():
    block = section(CANONICAL_BLOCK)
    assert block is not None
    assert "home_metro" in block
    assert "nothing here" not in block


def test_section_absent_when_no_addresses_heading():
    assert section("# Owner Profile\n\nProse only.\n") is None


def test_values_outside_the_block_are_ignored(tmp_path):
    # A `home_metro:` in prose or a later section must never feed the booking
    # check — only the canonical block counts.
    text = (
        "# Owner Profile\n\n"
        "- home_metro: Decoy Prose, XX\n\n"
        "## Addresses\n"
        "- home_metro: Nashville, TN\n\n"
        "## Notes\n"
        "- home_metro: Decoy Notes, XX\n"
    )
    profile = _write_profile(tmp_path, text)
    assert read_values("home_metro", path=profile) == ("Nashville, TN",)


# --- value parsing ----------------------------------------------------------


def test_reads_a_value_containing_commas(tmp_path):
    # The value itself carries the comma that separates city from region, which
    # is why a repeated line — not a separator — is how several metros are said.
    profile = _write_profile(tmp_path, CANONICAL_BLOCK)
    assert read_values("home_metro", path=profile) == ("Nashville, TN",)


def test_repeated_key_yields_every_value_in_file_order(tmp_path):
    profile = _write_profile(
        tmp_path,
        "## Addresses\n- home_metro: Nashville, TN\n- home_metro: Franklin, TN\n",
    )
    assert read_values("home_metro", path=profile) == ("Nashville, TN", "Franklin, TN")


def test_blank_value_is_unset(tmp_path):
    # A `- home_metro:` line with nothing after it must not become an
    # empty-string match that suppresses every unlabelled trip.
    profile = _write_profile(tmp_path, "## Addresses\n- home_metro:   \n")
    assert read_values("home_metro", path=profile) == ()


def test_tolerates_whitespace_variants(tmp_path):
    profile = _write_profile(tmp_path, "## Addresses\n-   home_metro :   Nashville, TN  \n")
    assert read_values("home_metro", path=profile) == ("Nashville, TN",)


def test_values_in_does_not_match_a_key_prefix():
    block = "\n- home_metro_notes: not this one\n- home_metro: Nashville, TN\n"
    assert values_in(block, "home_metro") == ("Nashville, TN",)


# --- tolerant reads ---------------------------------------------------------


def test_absent_profile_reads_as_unset(tmp_path, capsys):
    # Not every tier mounts /workspace/trusted; an unmounted profile is normal
    # and stays silent.
    assert read_values("home_metro", path=tmp_path / "nope.md") == ()
    assert capsys.readouterr().err == ""


def test_absent_block_reads_as_unset(tmp_path):
    profile = _write_profile(tmp_path, "# Owner Profile\n\nNo Addresses block.\n")
    assert read_values("home_metro", path=profile) == ()


def test_absent_key_reads_as_unset(tmp_path):
    profile = _write_profile(tmp_path, "## Addresses\n- current_home: 12 Example St\n")
    assert read_values("home_metro", path=profile) == ()


def test_unreadable_profile_reads_as_unset_with_diagnostic(tmp_path, capsys):
    profile = tmp_path / "user_profile.md"
    profile.write_bytes(b"## Addresses\n- home_metro: \xff\xfe not utf-8\n")
    assert read_values("home_metro", path=profile) == ()
    assert "unreadable" in capsys.readouterr().err


def test_env_override(tmp_path, monkeypatch):
    profile = _write_profile(tmp_path, CANONICAL_BLOCK)
    monkeypatch.setenv("USER_PROFILE_PATH", str(profile))
    assert read_values("home_metro") == ("Nashville, TN",)


# --- home-metro matching ----------------------------------------------------


def test_home_metro_names_are_normalized(tmp_path):
    profile = _write_profile(tmp_path, "## Addresses\n- home_metro:  NASHVILLE,   TN \n")
    assert home_metro_names(path=profile) == frozenset({"nashville, tn"})


def test_home_metro_names_empty_without_the_key(tmp_path):
    # The pre-#271 behaviour: nothing is home, so every trip is checked.
    profile = _write_profile(tmp_path, "## Addresses\n- home_airport: BNA\n")
    assert home_metro_names(path=profile) == frozenset()


def test_normalize_collapses_whitespace_and_case():
    assert normalize_location("  Nashville,   TN ") == normalize_location("nashville, tn")


def test_is_home_metro_matches_regardless_of_case_and_spacing():
    names = frozenset({"nashville, tn"})
    assert is_home_metro("Nashville, TN", names) is True
    assert is_home_metro("nashville,  tn", names) is True


def test_is_home_metro_rejects_a_different_place():
    names = frozenset({"nashville, tn"})
    assert is_home_metro("San Francisco, CA", names) is False


def test_is_home_metro_is_not_a_substring_test():
    # "Nashville, TN" must not swallow a different city that merely contains
    # the home label, nor a bare region.
    names = frozenset({"nashville, tn"})
    assert is_home_metro("East Nashville, TN 37206", names) is False
    assert is_home_metro("TN", names) is False


def test_unknown_destination_is_never_home():
    # An unlabelled trip is a trip whose destination we don't know. Reading
    # that as home would silence the check for exactly the trips it watches.
    names = frozenset({"nashville, tn"})
    assert is_home_metro("", names) is False
    assert is_home_metro(None, names) is False


def test_no_configured_metro_matches_nothing():
    assert is_home_metro("Nashville, TN", frozenset()) is False


# --- schema_version gate (stateful-artifacts) -------------------------------


def test_accepted_versions_read_normally(tmp_path):
    for version in sorted(ACCEPTED_SCHEMA_VERSIONS):
        profile = _write_profile(
            tmp_path,
            f"## Addresses\n- schema_version: {version}\n- home_metro: Nashville, TN\n",
        )
        assert read_values("home_metro", path=profile) == ("Nashville, TN",)


def test_absent_stamp_is_legacy_v1(tmp_path):
    # The field was introduced at v1, so a block without it has no earlier
    # shape to be — it reads normally.
    profile = _write_profile(tmp_path, "## Addresses\n- home_metro: Nashville, TN\n")
    assert read_values("home_metro", path=profile) == ("Nashville, TN",)


def test_forward_version_reads_as_unset_with_diagnostic(tmp_path, capsys):
    # This reader is the lagging side of a shape it doesn't know. The
    # no-prior-state path must be the safe one: no home metro means every trip
    # is checked, never a guess at the new shape.
    profile = _write_profile(
        tmp_path,
        "## Addresses\n- schema_version: 99\n- home_metro: Nashville, TN\n",
    )
    assert read_values("home_metro", path=profile) == ()
    err = capsys.readouterr().err
    assert "schema_version='99'" in err
    assert "upgrade" in err


def test_unparseable_version_reads_as_unset(tmp_path, capsys):
    # A stamp we cannot read is a stamp we cannot honour — not the same as
    # absent, which is the legacy shape.
    profile = _write_profile(
        tmp_path,
        "## Addresses\n- schema_version: draft-2\n- home_metro: Nashville, TN\n",
    )
    assert read_values("home_metro", path=profile) == ()
    assert "schema_version='draft-2'" in capsys.readouterr().err


def test_home_metro_names_empty_on_a_forward_block(tmp_path, capsys):
    profile = _write_profile(
        tmp_path,
        "## Addresses\n- schema_version: 99\n- home_metro: Nashville, TN\n",
    )
    assert home_metro_names(path=profile) == frozenset()
    capsys.readouterr()


def test_is_supported_version_contract():
    assert is_supported_version(None) is True
    assert all(is_supported_version(str(v)) for v in ACCEPTED_SCHEMA_VERSIONS)
    assert is_supported_version("99") is False
    assert is_supported_version("not-a-number") is False


def test_schema_version_returns_the_declared_value_verbatim():
    assert schema_version("\n- schema_version:  2 \n") == "2"
    assert schema_version("\n- current_home: 12 Example St\n") is None
