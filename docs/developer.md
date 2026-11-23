# Developer guide

Use a Python 3.14 environment with the pinned requirements for the suite. Tests use loopback peers
and temporary journals. Set `TMPDIR` to an absolute scratch directory.

```bash
export TMPDIR="$(mktemp -d)"
python tools/validate.py --quick
python -B tools/offline_validate.py tests/test_managed_session_stop.py tests/test_native_history_recovery.py
python -B tools/offline_validate.py tests tests_model
python -B tools/offline_validate.py tests_discovery
```

| Suite | Purpose | Requirements |
| --- | --- | --- |
| `tests` | Engine, simulated TLS peers, persistence, tools and recorded replays | `requirements-offline.txt` |
| `tests_model` | Generated ordering and fault sequences | `requirements-offline.txt` |
| `tests_ha_proxy` | Client behavior inside actual Home Assistant | `requirements-ha-proxy.txt` or `requirements-ha-proxy-2026.10.txt` |
| `tests_discovery` | Broader interaction and recovery search | `requirements-offline.txt` |
| `tests_installation`, `tests_rollback` | Opt-in tests with exact current and previously installed source snapshots | See [release and rollback](release-process.md) |

For each supported Home Assistant version, create a separate environment and use its Python:

```bash
python3.14 -m venv .venv-ha
.venv-ha/bin/python -m pip install -r requirements-ha-proxy.txt
.venv-ha/bin/python -B tools/offline_ha_validate.py tests_ha_proxy
```

Run the same command in an environment built from `requirements-ha-proxy-2026.10.txt` for the older supported
Core version. Keep pytest selection explicit. The full engine and model suites cover substantially more than
the two test files used by the engine runner’s default smoke command.

## Public replay files

The four replay files contain the same sanitized protocol frames already made public in the upstream project.
The public package keeps the frame order, direction, and coverage, with capture-source linkage removed.
The replays preserve observed message shapes and relative sequences. They are reserialized fixtures, and the
loopback tests drive the engine independently of any original recording. Put new synthetic cases in the ordinary
test code. Keep raw captures and installation journals outside Git.
