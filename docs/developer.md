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

## Routine engine checks

Pushes and pull requests collect the complete engine suite, then run the hosted shards with
`run_model_shard.py --review`. This selects 248 of the 1,368 current-resolution cases. It tests every
proof boundary, reply outcome and event order in both directions between 6 A and 32 A. It varies later
demand and checks all four directed pairs involving 16 A. The other engine and model cases run in full.
Each selected case must pass setup, execution, teardown, and the checks for the evidence it records.

Live release qualification requires the full engine collection, including all 1,368 current-resolution
cases. Use [the release process](release-process.md) for that run.

## Required settlement checks

Run these focused checks before review. Use the engine environment for the first command and run the second
in each supported Home Assistant environment.

```bash
python -B tools/check_control_settlement.py engine
python -B tools/check_control_settlement.py ha
```

The hosted checks run this selection before the routine engine checks and full Home Assistant suites. It covers
local and vendor-app Stop order, Pause overlap, closure across a refused receipt write and process replacement,
and exact Stop evidence across journal rotation. Scheduled cases exercise a clock rewind at cutoff, an
unavailable cutoff claim, and vendor registration changing during Start rollback. The Home Assistant cases
check the Stop receipt and reject stale canvas requests at the action and management write boundaries.

## Public replay files

The four replay files contain the same sanitized protocol frames already made public in the upstream project.
The public package keeps the frame order, direction, and coverage, with capture-source linkage removed.
The replays preserve observed message shapes and relative sequences. They are reserialized fixtures, and the
loopback tests drive the engine independently of any original recording. Put new synthetic cases in the ordinary
test code. Keep raw captures and installation journals outside Git.
