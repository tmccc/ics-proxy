#!/usr/bin/env python3
# Modified for the standalone iCS proxy repository.
"""Run actual HA suites against an explicitly provisioned HA runtime with networking limited to loopback."""

from __future__ import annotations

import os
import socket
import sys
from importlib.metadata import PackageNotFoundError, distribution, version
from importlib.util import find_spec
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
from offline_validate import network_guard as engine_guard  # noqa: E402

os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.dont_write_bytecode = True


# HA may use local Unix sockets, while every IP operation remains restricted by the engine's loopback audit.
def network_guard(event, args):
    if (
        event in ("socket.bind", "socket.connect", "socket.sendto", "socket.sendmsg")
        and args[0].family == socket.AF_UNIX
    ):
        return
    engine_guard(event, args)


# Confirm that the selected installation supplies the package and the version actually imported by the rehearsal.
def release_homeassistant_version() -> str:
    try:
        installed = distribution("homeassistant")
    except PackageNotFoundError:
        raise SystemExit("installation week requires an installed Home Assistant distribution") from None
    package = find_spec("homeassistant")
    expected = Path(installed.locate_file("homeassistant/__init__.py")).resolve()
    if package is None or package.origin is None or Path(package.origin).resolve() != expected:
        raise SystemExit("installation week Home Assistant import does not match its installed distribution")

    from homeassistant.const import __version__

    if __version__ != installed.version:
        raise SystemExit(
            f"installation week imported Home Assistant {__version__}. Metadata reports {installed.version}"
        )
    return __version__


# HA11 installs its compatibility aliases before pytest imports optional HTTP plugins.
def main() -> int:
    sys.addaudithook(network_guard)
    args = sys.argv[1:]
    if args == ["--print-ha-version"]:
        print(release_homeassistant_version(), flush=True)
        return 0
    expected = None
    if args and args[0].startswith("--expected-ha-version="):
        expected = args.pop(0).split("=", 1)[1]
    if expected is not None:
        actual = release_homeassistant_version()
        if actual != expected:
            raise SystemExit(f"installation week imported Home Assistant {actual}. Expected {expected}")

    import homeassistant  # noqa: F401
    import pytest

    if not Path(os.environ.get("TMPDIR", "")).is_absolute():
        raise SystemExit("set an absolute task TMPDIR")
    os.chdir(ROOT)
    plugins = ["pytest_asyncio.plugin", "pytest_freezer", "pytest_socket", "pytest_aiohttp.plugin", "pytest_timeout"]
    options = [item for plugin in plugins for item in ("-p", plugin)]
    options += [
        "-p",
        "no:cacheprovider",
        "--asyncio-mode=auto",
        "--disable-socket",
        "--allow-unix-socket",
        "--allow-hosts=127.0.0.1,::1",
        "-q",
    ]
    print(
        f"Isolated actual HA={expected or version('homeassistant')}",
        flush=True,
    )
    return pytest.main(options + args)


if __name__ == "__main__":
    raise SystemExit(main())
