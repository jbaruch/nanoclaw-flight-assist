"""Guards the registry's manifest limits before they reach the publish gate.

`tessl plugin lint` enforces a 1024-character ceiling on every `description` —
the plugin manifest's and each skill's frontmatter. That gate runs inside the
publish workflow, which fires only AFTER a merge to main, so an over-long
description passes every pre-merge check and then turns main red with a failed
publish. That is exactly what happened when the drive-engine description grew to
1118 characters (#231 / PR #232).

These tests run in the ordinary suite, so the limit is caught on the branch
where it can still be fixed cheaply.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / ".tessl-plugin" / "plugin.json"

# The registry's ceiling, mirrored from `tessl plugin lint`'s schema error
# ("Too big: expected string to have <=1024 characters"). Lint is the authority;
# this constant exists so the failure surfaces pre-merge rather than post.
MAX_DESCRIPTION_CHARS = 1024

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_DESCRIPTION_RE = re.compile(r"^description:[ \t]*(.+?)[ \t]*$", re.MULTILINE)


def _skill_files() -> list[Path]:
    declared = json.loads(MANIFEST.read_text(encoding="utf-8")).get("skills", [])
    return [REPO_ROOT / entry / "SKILL.md" for entry in declared]


def _frontmatter_description(path: Path) -> str | None:
    frontmatter = _FRONTMATTER_RE.match(path.read_text(encoding="utf-8"))
    if frontmatter is None:
        return None
    match = _DESCRIPTION_RE.search(frontmatter.group(1))
    if match is None:
        return None
    return match.group(1).strip().strip('"').strip("'")


def test_every_declared_skill_exists():
    """A manifest naming a skill directory with no SKILL.md fails lint too, and
    would make the description check below silently vacuous."""
    missing = [str(p.relative_to(REPO_ROOT)) for p in _skill_files() if not p.is_file()]
    assert missing == []


def test_the_manifest_description_is_within_the_registry_limit():
    description = json.loads(MANIFEST.read_text(encoding="utf-8"))["description"]
    assert len(description) <= MAX_DESCRIPTION_CHARS


@pytest.mark.parametrize("skill_file", _skill_files(), ids=lambda p: p.parent.name)
def test_each_skill_description_is_within_the_registry_limit(skill_file):
    description = _frontmatter_description(skill_file)
    assert description is not None, f"{skill_file} has no frontmatter `description`"
    assert len(description) <= MAX_DESCRIPTION_CHARS, (
        f"{skill_file.parent.name} description is {len(description)} chars; "
        f"`tessl plugin lint` rejects anything over {MAX_DESCRIPTION_CHARS} "
        "and that gate only runs after merge"
    )
