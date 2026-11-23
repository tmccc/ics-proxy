# iCS local proxy

A Linux service for a supported iCS electric vehicle charger, with a thin Home Assistant client and an optional
Solar Display 2 example. The service sits between the charger and its vendor backend. It retains session records,
enforces the commissioned current ceiling, and keeps vendor-app and charger-scheduled operation available when the
installation is ready.

**One control owner.** The proxy decides Start, session Stop, Pause, Return to app and schedule, and current changes.
Home Assistant and the display send requests to that proxy. The proxy checks the exact session and current charger
evidence before it sends an action, and separates the charger reply from proof that the requested state was reached.

| What you want to do | Where to start |
| --- | --- |
| Understand the control paths | [Code navigation](docs/code-navigation.md) |
| Prepare a supported installation | [Commissioning and operation](docs/commissioning.md) |
| Install the Home Assistant client | [Home Assistant setup](docs/home-assistant.md) |
| Run offline checks or review a change | [Developer guide](docs/developer.md) |
| Prepare a verified update | [Release and rollback](docs/release-process.md) |
| Connect a compatible touchscreen | [Solar Display 2 example](examples/solar-display-2/README.md) |

## Supported setup

The engine requires Linux, Python 3.14, OpenSSL, and the pinned Python dependencies in
`requirements-trial.txt`. It implements the native evidence and single-connector OCPP 1.6J behavior
used by the iCS installation this project was extracted from. Commissioning checks bind the actual
charger, firmware, existing current settings, profile behavior, saved plan and transaction history.
Each installation needs its own verified baseline. The native current preparation and release checks
must be completed before enabling managed current above the commissioning limit.

The client is exercised against Home Assistant Core 2026.10.4 and 2026.11.0. It communicates over an authenticated
management WebSocket on the proxy’s loopback interface. Home Assistant can restart without interrupting the
charger’s connection to the independent proxy.

## Run tests without a charger

From a checkout with Python 3.14:

```bash
python3.14 -m venv .venv
.venv/bin/python -m pip install -r requirements-offline.txt
export TMPDIR="$(mktemp -d)"
.venv/bin/python tools/validate.py --quick
.venv/bin/python -B tools/offline_validate.py tests tests_model
```

The engine tests use simulated peers on loopback. The full model suite is large. The
[developer guide](docs/developer.md) lists shorter starting selections and the two Home Assistant test environments.

## License

Apache 2.0. This project was extracted from OCPP Hub. See [LICENSE](LICENSE) and [NOTICE](NOTICE) for attribution.
