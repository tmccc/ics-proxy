"""Tagged releases select their own notes rather than whichever section happens to be first."""

import subprocess
import sys
from pathlib import Path

import pytest

from tools.release_notes import extract

ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = """# Changelog

## Unreleased

- Future changes.

## 1.10.0 — Current

- Tagged changes.

### Details

More tagged detail.

## 1.9.0

- Older changes.
"""


# Intent: select the requested version's complete body including its nested headings.
# Why: extracting the first section publishes Unreleased instead of the release being tagged.
@pytest.mark.parametrize("tag", ["v1.10.0", "1.10.0"])
def test_tagged_body_is_selected(tag: str) -> None:
    assert extract(CHANGELOG, tag) == "- Tagged changes.\n\n### Details\n\nMore tagged detail."


# Intent: display and package prerelease spellings select the same entry without collapsing numeric components.
# Why: matching must respect the version checker convention and must not select a different release.
def test_prerelease_heading_is_selected() -> None:
    assert extract("## Unreleased\nFuture\n## 1.8.0-rc2\nActual\n", "v1.8.0rc2") == "Actual"


# Intent: missing versions, empty entries and the Unreleased label fail rather than publish a fallback.
# Why: release notes are part of the tag and must not silently describe another version.
@pytest.mark.parametrize("text,tag", [(CHANGELOG, "v11.0.0"), ("## 1.10.0\n", "v1.10.0"), (CHANGELOG, "vUnreleased")])
def test_invalid_release_notes_fail(text: str, tag: str) -> None:
    with pytest.raises(ValueError):
        extract(text, tag)


# Intent: both supported invocations write the tagged notes and fail on an unknown tag.
# Why: a direct script invocation must resolve its repository imports independently of the working directory.
@pytest.mark.parametrize("direct", [False, True], ids=["module", "script"])
def test_release_notes_command(tmp_path: Path, direct: bool) -> None:
    changelog, output = tmp_path / "CHANGELOG.md", tmp_path / "notes.md"
    changelog.write_text(CHANGELOG)
    invocation = [str(ROOT / "tools/release_notes.py")] if direct else ["-m", "tools.release_notes"]
    command = [sys.executable, "-E", "-B", *invocation]
    working_directory = tmp_path if direct else ROOT
    result = subprocess.run(
        [*command, "v1.9.0", "--changelog", str(changelog), "--output", str(output)],
        cwd=working_directory,
        capture_output=True,
    )
    assert result.returncode == 0 and output.read_text() == "- Older changes.\n"
    output.unlink()
    result = subprocess.run(
        [*command, "v9.0.0", "--changelog", str(changelog), "--output", str(output)],
        cwd=working_directory,
        capture_output=True,
    )
    assert result.returncode != 0 and not output.exists()
