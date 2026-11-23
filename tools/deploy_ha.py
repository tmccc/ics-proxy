#!/usr/bin/env python3
# Modified for the standalone iCS proxy repository.
"""Verify and install the iCS Home Assistant client inside an explicitly selected Docker container."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import io
import json
import re
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import ha_restart  # noqa: E402

INTEGRATION = Path("custom_components/ics_proxy")
BUILD_FILE = "BUILD"


@dataclass(frozen=True)
class RestartAccess:
    """Private credentials for the verified Core process inside the chosen container."""

    token: str = field(repr=False)
    container_port: int = 8123


# Read KEY=value pairs from a .env text; comments and blank lines are ignored and quotes are stripped.
# Deployment settings live in a git-ignored file and must never be exported to child processes.
def parse_env(text: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip("\"'")
    return env


# List the files to deploy relative to the integration folder: tracked and untracked-but-not-ignored, no caches.
def relative_files(root: Path = ROOT, integration: Path = INTEGRATION) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-c", "-o", "--exclude-standard", "-z", "--", str(integration)],
        cwd=root,
        capture_output=True,
        check=True,
    ).stdout.decode()
    prefix = integration.as_posix() + "/"
    return sorted(p[len(prefix) :] for p in out.split("\0") if p.startswith(prefix) and "__pycache__" not in p)


# Stamp the commit and mark local tracked or untracked component code shipped by an unchecked deploy.
def write_build_stamp(root: Path = ROOT, integration: Path = INTEGRATION) -> str:
    head = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=normal", "--", str(integration)],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    stamp = head + ("+dirty" if dirty else "")
    (root / integration / BUILD_FILE).write_text(stamp + "\n")
    return stamp


# Run the CI-equivalent checks and abort the deploy when they fail.
# A failing tree must never reach the server; one command gives the same answer as CI.
def run_checks(
    root: Path = ROOT,
    collection_executors: Path | None = None,
    *,
    snapshot: Path | None = None,
    domain: str = "ics_proxy",
) -> None:
    command = [sys.executable, str(root / "tools" / "validate.py")]
    if domain != "ics_proxy":
        raise SystemExit("This installer supports only the iCS proxy client.")
    command += ["--deployment-target", "ics-client"]
    if collection_executors is not None:
        command += ["--collection-executors", str(collection_executors)]
    if snapshot is not None:
        command += ["--validated-snapshot", str(snapshot)]
    result = subprocess.run(command, cwd=root)
    if result.returncode:
        raise SystemExit("Checks failed; nothing was deployed. Fix them (or use --skip-checks if you know why).")


# Package the exact checked Home Assistant client files.
def make_archive(directory: Path, files: list[str], pinned: dict[str, str]) -> tuple[bytes, dict[str, str]]:
    data = io.BytesIO()
    digests = {}
    with tarfile.open(fileobj=data, mode="w") as package:
        for name in files:
            path = directory / name
            if path.is_symlink() or not path.is_file():
                raise SystemExit("Deployment accepts regular files only")
            content = path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            if name in pinned and digest != pinned[name]:
                raise SystemExit("Validated integration bytes changed before packaging")
            info = tarfile.TarInfo(name)
            info.mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
            info.size = len(content)
            package.addfile(info, io.BytesIO(content))
            digests[name] = digest
    return data.getvalue(), digests


# Select the exact validated inventory, or the explicitly requested unchecked working tree.
def prepare_archive(
    integration: Path, scratch: Path, *, skip_checks: bool, collection_executors: Path | None
) -> tuple[Path, bytes, dict[str, str]]:
    source = ROOT
    pinned: dict[str, str] = {}
    if skip_checks:
        files = relative_files(source, integration)
        stamp = write_build_stamp(source, integration)
    else:
        run_checks(source, collection_executors, snapshot=scratch, domain=integration.name)
        metadata = json.loads((scratch / "snapshot.json").read_text())
        source = scratch / "source"
        prefix = integration.as_posix() + "/"
        pinned = {
            row["path"][len(prefix) :]: row["sha256"] for row in metadata["files"] if row["path"].startswith(prefix)
        }
        files = sorted(pinned)
        stamp = metadata["commit"][:12]
        (source / integration / BUILD_FILE).write_text(stamp + "\n")
    if not files:
        raise SystemExit(f"No files found under {integration}.")
    print(f"Build {stamp}")
    data, digests = make_archive(source / integration, sorted({*files, BUILD_FILE}), pinned)
    return source, data, digests


# Read credentials locally and transmit the token over stdin only for an explicitly requested restart.
def restart_access(env: dict[str, str], path: Path | None) -> RestartAccess:
    try:
        if path is None:
            access = RestartAccess(env.get("HA_API_TOKEN", ""))
        else:
            private = json.loads(path.read_text())
            access = RestartAccess(Path(private["token_file"]).read_text().strip(), private.get("container_port", 8123))
    except KeyError, OSError, TypeError, ValueError:
        raise SystemExit("The private Home Assistant API settings could not be read.") from None
    if not access.token or "\n" in access.token or "\r" in access.token:
        raise SystemExit("A Home Assistant API token is needed for --restart.")
    return access


# Pin Docker operations to the inspected container rather than resolving its name again after installation.
def container_id(name: str) -> str:
    command = ["docker", "inspect", "--type", "container", "--format", "{{.Id}}", name]
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    value = result.stdout.strip()
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise SystemExit("Cannot resolve the selected Home Assistant container.")
    return value


# Keep a container install and the optional restart in the same process and under the same lock.
def deploy_container(
    container: str, source: Path, domain: str, archive: bytes, expected: dict[str, str], access: RestartAccess | None
) -> None:
    installer = (source / "tools/ha_container_deploy.py").read_text()
    command = ["docker", "exec", "-i", container, "python", "-c", installer, domain, json.dumps(expected)]
    if access is not None:
        port = access.container_port
        if type(port) is not int or not 1 <= port <= 65535:
            raise SystemExit("The Home Assistant container API port is invalid.")
        command.extend((str(port), (source / "tools/ha_restart.py").read_text()))
        archive = access.token.encode() + b"\n" + archive
    try:
        subprocess.run(command, input=archive, check=True)
    except subprocess.CalledProcessError as error:
        raise SystemExit(error.returncode) from None


# Resolve the optional restart confirmation before modifying the selected installation.
def confirm_restart(assume_yes: bool, batch: bool) -> bool:
    if assume_yes:
        return True
    if batch:
        raise SystemExit("A batch restart also requires --yes.")
    question = "Restart Home Assistant now? Its dashboards and automations will briefly be unavailable. [y/N] "
    return input(question).strip().lower() == "y"


# Validate and dry-run the selected container before packaging or requesting any live change.
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", required=True, help="explicit local Home Assistant Docker container")
    parser.add_argument("--domain", choices=("ics_proxy",), default="ics_proxy")
    parser.add_argument("--batch", action="store_true", help="never prompt")
    parser.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    parser.add_argument("--skip-checks", action="store_true", help="explicitly bypass release validation")
    parser.add_argument("--restart", action="store_true", help="validate the HA config and restart Home Assistant")
    parser.add_argument("--yes", action="store_true", help="confirm --restart")
    parser.add_argument("--ha-api-config", type=Path, help="private configuration with token_file and optional port")
    parser.add_argument("--collection-executors", type=Path, help="trusted qualification collection executor map")
    args = parser.parse_args()
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", args.container) is None:
        raise SystemExit("Invalid container name")
    if args.dry_run:
        files = set(relative_files(ROOT, INTEGRATION)) | {BUILD_FILE}
        print(f"Dry run: {args.domain}, container {args.container}, {len(files)} files; no changes")
        return 0
    selected = container_id(args.container)
    access = None
    if args.restart and confirm_restart(args.yes, args.batch):
        settings = ROOT / ".env"
        env = parse_env(settings.read_text()) if settings.exists() else {}
        access = restart_access(env, args.ha_api_config)
    with tempfile.TemporaryDirectory() as tmp:
        source, archive, expected = prepare_archive(
            INTEGRATION, Path(tmp), skip_checks=args.skip_checks, collection_executors=args.collection_executors
        )
        deploy_container(selected, source, args.domain, archive, expected, access)
    if access is not None:
        print("Verified Home Assistant is running in a new Core process.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ha_restart.RestartError as error:
        raise SystemExit(str(error)) from None
    except http.client.HTTPException, OSError, ValueError:
        raise SystemExit("Home Assistant access failed.") from None
