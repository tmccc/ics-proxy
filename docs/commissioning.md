# Commissioning and operation

The supported setup is an iCS charger with one connector, single-phase charging, and an original saved user
limit and installer ceiling of 32 A. Verify the charger, firmware, and approved current against its own
installation evidence. Commissioning needs an authorized idle charger and a known working route to its original
backend. Leave the charger on its original setup while preparing and inspecting the evidence.

The read-only native charger checks use plain HTTP with Basic authentication. Keep that traffic on a trusted,
isolated local network segment. See [security](../SECURITY.md) for the separate OCPP TLS connections.

The configuration example and shared commissioning tools use the `trial` name. Baseline capture serves both
modes. `trial_proxy.py run` starts a time-limited trial, and `managed_proxy.py serve` runs the persistent service.

1. Install Python 3.14 and OpenSSL on Linux. Create a virtual environment and install `requirements-trial.txt`.
2. Use [the configuration example](../packaging/trial/trial.json.example) to prepare `/etc/ics-proxy/proxy.json`.
   Set the paths and connection settings for this installation. Keep `max_current_a` at `6`. The managed service
   uses the verified capability saved in its journal to allow selections above 6 A.
3. Keep the configuration, listener TLS key, authentication files, native credentials, native bounded specification,
   baseline, and management token outside the checkout. Give the service account ownership of the private files
   and restrict access to that account. Keep the writable journal and evidence in a private state directory.
4. Keep the charger on its original route and plug in the vehicle. Independently verify that the session is
   closed and the output is zero. Run
   `python -B tools/trial_proxy.py capture-bounded-baseline --config /etc/ics-proxy/proxy.json` as the service
   account. The read-only capture saves the baseline and the original charger configuration to
   the configured baseline path and its `.original-config.json` companion. Prepare the native bounded specification
   from those files and the installation’s private commissioning evidence.
5. Run the offline preflight as the service account before changing the charger route.

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

## First handover

1. After the preflight passes, use the authorized charger controls to set both the saved and effective user
   limits to 6 A. Keep the original backend route and all other saved settings unchanged. Confirm that the car
   is plugged in and idle, the native session is closed, and the charger reports zero output.
2. Start the proxy service while the charger still uses the original route. Wait for the proxy to confirm the
   charger listener is open. In the systemd example, the service reports ready with the status
   `Waiting for verified charger and owner control` at this point.
3. Point the charger at the configured proxy route using the four `trial_route` fields. Keep the saved and
   effective user limits at 6 A through the first charger Boot. Verify the proxy binds that Boot and reports
   the expected zero hold before authorizing normal operation.

Confirm the actual profile, journal and saved resources before authorizing normal operation. The separate
`tools/prepare_managed_current.py` action requires exclusive journal ownership and the verified zero hold before
preparing the supported native 32 A capability. Use its offline `check` first. After successful preparation and
verification, the service can offer whole-amp selections from 6 A to 32 A when the charger is idle and the
current selection checks pass.

## App and schedule control

The proxy verifies the complete commissioned charger schedule, including its default charging mode. In
**App / schedule**, the charger can run that saved schedule under the selected limit. Creating, editing, or
deleting charger schedules through the proxy is disabled. For a current session that the proxy can match to
the vendor’s request, **Fast** is the only vendor mode change the proxy permits. Plan changes with the installer
before editing charger settings. A changed schedule or configuration can block operation until verification.

**Return to app / schedule** releases a verified local Pause and gives control back to the app and the commissioned
schedule while the charger still connects through the proxy. The selected proxy current limit continues to apply.

## Run the service

The [systemd example](../packaging/ics-proxy.service.example) runs the persistent service with a dedicated user,
protected state files, and systemd watchdog support. Adapt the executable, checkout and configuration paths.
Management settings use `host`, `port`, and `token_file` in a private JSON file. The host is `127.0.0.1`.
The token file contains a JSON object with `token`, a private 64-character lowercase hexadecimal value.

Run `health` and `incident` as the service account that owns the private configuration and state files. Both
commands read saved records. `incident` reports the most recent local Start by default.

```bash
python tools/managed_proxy.py health --config /etc/ics-proxy/proxy.json
python tools/managed_proxy.py incident --config /etc/ics-proxy/proxy.json
python tools/managed_proxy.py incident --config /etc/ics-proxy/proxy.json --kind stop
python tools/managed_proxy.py incident --config /etc/ics-proxy/proxy.json --kind vendor-stop
```

The Start report includes a matching reply saved separately before it reached the normal command record.
The Stop reports select the latest retained local session-only Stop or vendor-app Stop, respectively.
They show possible dispatch, any recorded reply, and evidence of closure for that exact session. The reports label the
latest service status and recent process starts separately so those observations can be compared with the request.
A command can remain uncertain until the charger reports more evidence.

## Recover availability

1. Check Home Assistant’s **Control mode** and run `health`. Use `health_blockers` to distinguish a vendor outage
   from a stopped service, disconnected charger, stale native readings, or an unsettled command. Check the
   service manager and restore the affected connection. Keep the private journal and baseline. Charging may
   continue when readings are unavailable. Verify session closure independently before treating it as stopped.
2. The proxy retries the vendor connection while keeping its charger listener open. During a vendor outage, fresh
   charger evidence can still make local controls available, and a local Start requires valid cached authorization.
   After a restart or charger reconnect, the proxy needs a fresh charger Boot and independent evidence before it
   enables positive charging actions. A clean automatic Pause caused by a failed vendor connection can release
   when the vendor returns and recovery checks pass. A Pause you requested stays in effect until you request
   Return or deliberately start a new local session.
3. If the proxy remains at **Needs attention**, use `health` and, for a Start or Stop, the relevant `incident`
   report. Where Pause is available, request it and wait for **Paused locally** and independently verified zero
   and session closure. Use **Return to app / schedule** when it becomes available and you are ready for the app
   and schedule to control charging again. If the proxy still awaits evidence, leave the hold in effect.

For an outcome that needs operator reconciliation, use the tool for the exact recorded issue. Start with `--help`
and `check`. The command reconciliation tools also have an `inspect` action. Reconciliation requires a stopped
service, exclusive access to its journal, verified closed zero, and the evidence required for the specific issue.
Keep a private state backup before writing reconciliation records. The tools cover
[native history](../tools/acknowledge_native_history.py),
[a cloud-accepted Start awaiting a mapping](../tools/reconcile_managed_delivery.py), and
[other specific unsettled commands or reports](../tools/reconcile_managed_debt.py).

## Roll back software or restore the original setup

[Software rollback](release-process.md) concerns the proxy build and its journal while the charger stays on the
proxy route. Keep the state directory across upgrades. The rollback checker covers specific historical build
and journal combinations. The release rehearsal tests the candidate and its verified predecessor on a simulated
installation with the same journal.

Direct operation on the original vendor backend requires restoring the charger route and settings after verified
cleanup. The managed service has no supported procedure for that operation. The 6 A commissioning trial has a
profile cleanup command that leaves the charger at the verified 6 A limit and leaves route restoration pending.
Selecting 6 A in Home Assistant leaves the commissioned 32 A capability in place, and the trial cleanup refuses
that capability. Arrange an installation-specific restoration procedure with the installer before disconnecting
the proxy or changing the saved route or current settings.
