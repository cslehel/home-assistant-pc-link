"""Release consistency: one version everywhere, and notes for it in CHANGELOG.md."""
from __future__ import annotations

import json
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import release_notes  # noqa: E402

import pclink  # noqa: E402


def _read(*parts: str) -> str:
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def test_version_is_the_same_everywhere():
    manifest = json.loads(_read("custom_components", "pclink", "manifest.json"))["version"]
    pyproject = re.search(r'^version\s*=\s*"([^"]+)"', _read("pyproject.toml"), re.M).group(1)
    assert pclink.__version__ == manifest == pyproject


def test_changelog_has_notes_for_the_current_version():
    notes = release_notes.section(_read("CHANGELOG.md"), "v" + pclink.__version__)
    assert len(notes.strip()) > 100
    assert f"PCLink-{pclink.__version__}-windows-x64.zip" in notes  # the download it describes


def test_release_notes_stop_at_the_next_version():
    text = "# Changelog\n\n## [1.1.0] - 2027-01-01\n\nnew things\n\n## [1.0.0] - 2026-01-01\n\nold things\n"
    assert release_notes.section(text, "v1.1.0") == "new things\n"
    assert release_notes.section(text, "1.0.0") == "old things\n"
    with pytest.raises(LookupError):
        release_notes.section(text, "v2.0.0")


def test_release_notes_links_are_absolute():
    """Relative links do not resolve on the GitHub release page."""
    notes = release_notes.section(_read("CHANGELOG.md"), pclink.__version__)
    for target in re.findall(r"\]\(([^)]+)\)", notes):
        assert target.startswith("https://"), target
