# Commissioning and operation

The service relies on independent native charger readings and a verified original baseline. Commissioning needs
an authorized idle charger, a known working route to its original backend, and an agreed current ceiling.
Leave the charger on its original setup while preparing and inspecting the evidence.

1. Install Python 3.14 and OpenSSL on Linux. Create a virtual environment and install `requirements-trial.txt`.
2. Prepare a private configuration using [the trial example](../packaging/trial/trial.json.example). Choose the
   charger’s existing approved current limits and substitute its actual installation values locally.
3. Keep the listener TLS key, authentication files, native credentials, native bounded specification, baseline,
   and management token outside the checkout. Install them for the service account with restrictive permissions.
   Keep the writable journal and evidence in a private state directory.
4. Run `python -B tools/trial_proxy.py capture-bounded-baseline --config /etc/ics-proxy/proxy.json` with the charger
   on its original route and at independently verified closed zero. The read-only capture saves the baseline and
   the original charger configuration to the configured baseline path and its `.original-config.json` companion.
   Prepare the native bounded specification from those files and the installation's private commissioning evidence.
5. Run the offline preflight against those files before the route is changed:

```bash
python -B tools/managed_proxy.py check --config /etc/ics-proxy/proxy.json \
  --native-bounded-spec /etc/ics-proxy/native-spec.json \
  --management-config /etc/ics-proxy/management.json
```

The native specification is a JSON object with six fields.

| Field | Value |
| --- | --- |
| `hardware_identity` | The native `bPSN`, matching the configured hardware identity |
| `original_configuration` | The complete JSON object from the saved original charger configuration |
| `trial_route` | The four proposed native route fields, `sDomain`, `sPort`, `sURL`, and `allowSelfSign`, as strings. Use the configured listener name and port. Adding the charger identity to `sURL` must produce the configured charger path. |
| `saved_schedule_digest` | The fingerprint of the complete native `SETPLAN` response from the commissioning evidence |
| `capability_digest` | The independently established capability digest from the private commissioning record. The service separately verifies current capabilities before applying the station profile. |
| `boot_digest` | The fingerprint of the complete observed OCPP `BootNotification` payload for this charger |

The saved schedule and Boot fingerprints use [the same deterministic JSON encoding and SHA-256 function as the
proxy](../ics_proxy_core/engine/store.py#L40). Use the validated private evidence from the selected charger.
The baseline capture supplies the original configuration. The native schedule, commissioning capability digest,
and OCPP Boot evidence must be obtained separately before commissioning.

The installation guides start with commissioning at a restricted current. The independent evidence must confirm
the actual profile, journal and saved resources before authorizing normal operation. The separate
`tools/prepare_managed_current.py` action requires exclusive journal ownership and the already verified zero hold
before preparing the supported native capability. Use its read-only checks first. Supplying an arbitrary higher
current in a configuration file cannot establish that capability.

## Run the service

The [systemd example](../packaging/ics-proxy.service.example) runs the persistent service with a dedicated user,
protected state files, and systemd watchdog support. Adapt the executable, checkout and configuration paths.
Management settings use `host`, `port`, and `token_file` in a private JSON file. The host is `127.0.0.1`.
The token file contains a JSON object with `token`, a private 64-character lowercase hexadecimal value.

Use `managed_proxy.py health` for the saved status or `managed_proxy.py incident` to inspect classified information
about a particular problem. A missing latest observation or a pending charger reply can hold or defer controls.
Keep the state directory across upgrades. An offline reconciliation requires the service to be stopped and the
corresponding evidence of the exact action. See [release and rollback](release-process.md).
