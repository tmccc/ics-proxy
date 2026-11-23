"""Bind each hosted Home Assistant suite to its declared runtime."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from tools import run_hosted_ha as hosted


# Intent: bind each hosted matrix row to the matching Home Assistant requirement and CLI version argument.
# Why: each test suite is expected to exercise a specific supported Home Assistant version.
def test_hosted_matrix_binds_both_home_assistant_pins():
    root = Path(__file__).resolve().parents[1]
    job = yaml.safe_load((root / ".github/workflows/checks.yml").read_text())["jobs"]["ha-layer"]
    matrix = job["strategy"]["matrix"]["include"]
    assert len(matrix) == len(hosted.HA_SERIES)
    assert {row["collection_suite"] for row in matrix} == set(hosted.HA_SERIES)
    for row in matrix:
        pin = row["expected_ha_version"]
        assert pin.rsplit(".", 1)[0] == hosted.HA_SERIES[row["collection_suite"]]
        assert f"homeassistant=={pin}" in (root / row["requirements"]).read_text().splitlines()
    version_argument = '--expected-ha-version "${{ matrix.expected_ha_version }}"'
    assert any(version_argument in step.get("run", "") for step in job["steps"])


# Intent: require the declared Home Assistant version during collection, execution, and audit.
# Why: results collected or executed under a different version cannot qualify the selected suite.
@pytest.mark.parametrize("mismatch", [None, "collection", "execution", "suite", "installed"])
def test_hosted_runner_requires_the_same_home_assistant_in_both_phases(tmp_path, monkeypatch, mismatch):
    pin = "2026.11.0"
    output = tmp_path / "hosted"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_hosted_ha.py",
            "--suite",
            "ha-proxy",
            "--expected-ha-version",
            "2026.10.4" if mismatch == "suite" else pin,
            "--output",
            str(output),
        ],
    )
    stages = []
    runtime = {"python": "3.14", "python_sha256": "a" * 64, "packages": {"homeassistant": pin}}
    swapped = runtime | {"packages": {"homeassistant": "2026.10.4"}}

    # Record each phase's environment and synthesize version evidence for the chosen mismatch.
    def run(command, *, cwd, env):
        assert json.loads(env["QUALIFICATION_EXPECTED_PACKAGES"]) == {"homeassistant": pin}
        stage = Path(command[2]).stem
        stages.append(stage)
        if stage == "qualification_collection":
            collected = swapped if mismatch == "collection" else runtime
            (output / "collection.json").write_text(json.dumps({"runtime": collected}))
        else:
            assert stage == "offline_ha_validate"
            rows = [
                {"event": "worker_namespace", "runtime": runtime},
                {"event": "session_finish", "runtime": swapped if mismatch == "execution" else runtime},
            ]
            (output / "results.jsonl").write_text("\n".join(json.dumps(row) for row in rows))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(hosted.subprocess, "run", run)
    monkeypatch.setattr(hosted, "version", lambda _: "2026.10.4" if mismatch == "installed" else pin)
    monkeypatch.setattr(hosted, "hosted_collection", lambda *args: ["expected"])
    monkeypatch.setattr(hosted, "manifest", lambda path, suite: path.write_text(""))
    verified = []
    monkeypatch.setattr(hosted, "verify_execution", lambda *args: verified.append(True))

    if mismatch in ("suite", "installed"):
        with pytest.raises(SystemExit) as error:
            hosted.main()
        assert error.value.code == 2 and stages == [] and not output.exists()
    elif mismatch == "collection":
        with pytest.raises(RuntimeError, match="expected Home Assistant version"):
            hosted.main()
        assert stages == ["qualification_collection"]
    else:
        if mismatch == "execution":
            with pytest.raises(RuntimeError, match="pinned collection runtime"):
                hosted.main()
        else:
            assert hosted.main() == 0
        assert stages == ["qualification_collection", "offline_ha_validate"]
    assert bool(verified) is (mismatch is None)
