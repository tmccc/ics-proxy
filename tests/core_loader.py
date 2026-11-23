# Modified for the standalone iCS proxy repository.
"""Select the installed engine for a cross-version installation rehearsal."""

import importlib.util
import sys
from pathlib import Path


# An installed source export can predate the standalone repository layout.
def installed_core(root: Path) -> Path:
    standalone = root / "ics_proxy_core"
    return standalone if standalone.is_dir() else root / "custom_components" / "ocpp_hub" / "core"


# Bind the selected source before any test fixture imports engine types.
def load_test_core(root: Path) -> None:
    directory = installed_core(root)
    _load("ics_proxy_core", directory)
    if directory.name != "ics_proxy_core":
        _load("ics_proxy_core.engine", directory / "prototype")
        from ics_proxy_core import control

        sys.modules["ics_proxy_core.constants"] = control


# Load exactly one package name for the chosen source so imported types share their identity.
def _load(name: str, directory: Path) -> None:
    spec = importlib.util.spec_from_file_location(
        name, directory / "__init__.py", submodule_search_locations=[str(directory)]
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
