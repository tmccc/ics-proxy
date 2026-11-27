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
   Use a separate file or a SQLite backup for journal backups. The service and maintenance tools refuse
   hard-linked state files.
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

Plan charger firmware updates with the installer. An update can change the Boot message, saved settings or
schedule, or the format of readings the proxy checks. These changes can block charging or latch a fault.
Keep the existing baseline and journal, and verify the updated firmware against the commissioning requirements
before resuming proxy control.

## First handover

1. After the preflight passes, use the authorized charger controls to set both the saved and effective user
   limits to 6 A. Keep the original backend route and all other saved settings unchanged. Confirm that the car
   is plugged in and idle, the native session is closed, and the charger reports zero output.
2. Start the proxy service while the charger still uses the original route. Wait for the proxy to confirm the
   charger listener is open. In the systemd example, the service reports ready with the status
   `Waiting for verified charger and owner control` at this point.
3. Choose an idle commissioning window before changing the route. Leave any vendor-app Start until after
   verification and check when the saved schedule can next start charging.
4. Point the charger at the configured proxy route using the four `trial_route` fields. Keep both user limits
   at 6 A. The proxy verifies the first Boot and establishes a 0 A hold. Once the connection and recovery
   checks pass, the proxy automatically enables app and schedule control at 6 A. The initial hold may pass
   between status updates.
5. Confirm the verified Boot, the 6 A ceiling, and the expected profile, journal and saved resources.

To prepare the native 32 A capability later, request **Pause charger** and verify the closed session and 0 A
hold. Stop the service so the separate `tools/prepare_managed_current.py` action can take exclusive journal
ownership. Use its offline `check` first. After successful preparation and verification, the service can offer
whole-amp selections from 6 A to 32 A when the charger is idle and the current selection checks pass.

## App and schedule control

The proxy verifies the complete commissioned charger schedule, including its default charging mode. In
**App / schedule**, the charger can run that saved schedule under the selected limit. Creating, editing, or
deleting charger schedules through the proxy is disabled. For a current session that the proxy can match to
the vendor’s request, **Fast** is the only vendor mode change the proxy permits. Plan changes with the installer
before editing charger settings. A changed schedule or configuration can block operation until verification.

**Return to app / schedule** releases a verified local Pause and gives control back to the app and the commissioned
schedule while the charger still connects through the proxy. The selected proxy current limit continues to apply.

### Local Start during a vendor outage

An installation can opt in by setting `owner_start_tag` in the private service configuration. Choose a dedicated
local identity of 1–20 letters, numbers, hyphens, or underscores. It must be distinct from `local_tags` and the
identities used by the commissioned schedule. Keep this setting private with the rest of the charger configuration.

When iCS is disconnected, a normal Start through authenticated management can use that identity for one request.
The proxy verifies the current charger connection, idle state, selected ceiling, and existing command outcomes
before issuing the Start. Use **Stop** to close that exact session, or **Pause charger** to keep a zero hold after
closure. Cloud availability and charging readiness are reported separately. Timed tests and Return to app
control follow their existing authorization and recovery rules.

Sessions started under this local authorization stay in the proxy journal with their charger reports and are
available through the proxy's history, flagged as `local_only`. Their energy totals are available there.
These sessions do not appear in the iCS app, because iCS did not authorize them. Keep the journal when updating
the service. A downgrade involving locally authorized sessions needs a rehearsed build and journal pair that
supports those records.

### The midnight schedule during a vendor outage

To opt in to automatic overnight Starts during an iCS outage, set `owner_schedule_timezone` to an IANA time
zone such as `Europe/London` in the private service configuration, alongside the dedicated `owner_start_tag`.
The proxy recognizes a single commissioned daily midnight-to-06:00 plan, using local civil time on
clock-change days. It checks Linux kernel NTP synchronization at admission and immediately before sending
Start. An unavailable or unsynchronized clock blocks a new automatic Start. The kernel can continue to
report synchronization during holdover.

The proxy holds charging at zero while it opens the session. It uses the exact charger transaction number
to arm a charger-side timer and can offer current after it records the timer acknowledgement. While connected,
the proxy also requests zero and Stop at cutoff if the session is still open. The journal preserves the cutoff
and session identity for recovery, and the proxy verifies closure separately from the charger’s reply.
A replacement connection requests Stop for the exact session if the timer belongs to the previous connection.

The [overnight verification procedure](release-process.md#qualify-the-overnight-outage-schedule) covers
direct observation of the charger’s cutoff while the proxy is offline.

## Run the service

The [systemd example](../packaging/ics-proxy.service.example) runs the persistent service with a dedicated user,
protected state files, and systemd watchdog support. Adapt the executable, checkout and configuration paths.
Management settings use `host`, `port`, and `token_file` in a private JSON file. The host is `127.0.0.1`.
The token file contains a JSON object with `token`, a private 64-character lowercase hexadecimal value.

Choose an idle window to stop or restart the proxy. During a normal shutdown, it tries to stop any open session
and verify both closure and zero output before exiting. If either check cannot be completed, the saved shutdown
status reports `shutdown_uncertain`. Verify the actual charger state before assuming the session closed.

Run `health` and `incident` as the service account that owns the private configuration and state files. Both
commands read saved records. `incident` reports the most recent local Start by default.
It can also read a cleanly stopped, checkpointed journal in a read-only backup directory using a temporary private copy.

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

To select a specific local Stop, add `--request-reference` followed by the `request_reference` from the
[Home Assistant action response](home-assistant.md). A readable journal with no matching request reports
`journal.available` as `true` and `journal.request` as `null`.

## Recover availability

Start with Home Assistant’s **Control mode** and the `health` command above. Keep the private journal and
baseline so the proxy can recover the original session and commands. Use `health_blockers` and `fault` in the
saved status to identify what needs attention.

| What you see | What to do |
| --- | --- |
| `service_not_running` or `status_stale` | Check the service manager. If shutdown was uncertain, check whether the charger still has an open session. |
| `charger_disconnected` or `boot_unverified` | Check the charger connection. After a restart or reconnect, the proxy needs a fresh charger Boot and independent readings before it permits charging again. |
| `cloud_disconnected` | Check the vendor connection. The proxy retries it while keeping the charger listener open. Fresh charger evidence can make local controls available, and local Start requires valid cached authorization. A clean automatic Pause can release when the vendor returns and recovery checks pass. |
| `native_stale` or `native_observation_unavailable` | Check the charger’s local network connection and HTTP readings. Charging may continue while readings are missing. |
| `command_outcome_unknown` or **Needs attention** after Start or Stop | Read the matching `incident` report above. For a Stop, choose `stop` for a local request or `vendor-stop` for a vendor-app request. Verify the charger’s actual state before acting on an uncertain result. |

In normal app or schedule operation, three unavailable or stale full supervision checks trigger a request for
a 0 A hold. Timing depends on how long the checks take. A session can stay open at 0 A and resume when the
recovery checks pass.

After a Stop during a vendor outage, the charger’s local status can still show that exact session open at
zero after the proxy has recorded its closing report. On restart, the proxy can open its listener in a
restricted state so recovery can continue. It waits for the local status to catch up before releasing
charging control.

If the proxy restarts before saving the result of a vendor authorization check, that tag needs a fresh
vendor decision before its older approval can be used again. Other tags keep their own authorization.

Three refused supervision checks can latch a fault when the charger returns conflicting or unsupported data.
A missing reading leaves that refusal count in place. A valid, accepted check clears the count. A strict refusal
while verifying a new charger connection can latch immediately. If the fault persists, verify the configured
charger, its firmware and settings against the commissioned baseline and review the recorded fault before proceeding.

If you need charging to stop and **Pause charger** is available, request it and wait for **Paused locally**.
Independently verify zero output and session closure. If the proxy still awaits evidence, leave the hold in
effect. A Pause you requested stays in effect until you deliberately start a new local session or use
**Return to app / schedule** when it becomes available.

### Reconcile a recorded outcome

Choose the tool that matches the recorded issue. Start with `--help` and `check` to verify the inputs. For a
command or report, `inspect` shows the exact candidate after the service has stopped.

| Recorded issue | Tool |
| --- | --- |
| Missing native history or unconfirmed timed Start attempts tied to history recovery | [`acknowledge_native_history.py`](../tools/acknowledge_native_history.py) |
| A vendor-accepted Start is awaiting a transaction mapping | [`reconcile_managed_delivery.py`](../tools/reconcile_managed_delivery.py) |
| A specific unsettled command or report needs independent evidence | [`reconcile_managed_debt.py`](../tools/reconcile_managed_debt.py) |

To see which unsettled commands or reports are eligible for reconciliation, run the read-only inspection as
the service account with the proxy stopped. Each result lists the candidate and the fields you must verify
independently.

```bash
python -B tools/reconcile_managed_debt.py inspect --config /etc/ics-proxy/proxy.json \
  --native-bounded-spec /etc/ics-proxy/native-spec.json
```

Reconciliation requires a stopped service, exclusive access to its journal, a verified closed session at zero
output, and independent evidence for the specific issue. Keep a private state backup before writing
reconciliation records.

If a recorded history fault was caused only by missing records and those records have all returned unchanged,
the history tool can clear that exact fault while keeping strict history checking in place. If startup finds
missing history and a saved vendor acknowledgement for a Stop report, the proxy can finish recording that
report as acknowledged before checking whether history repair is available.

When an uncertain vendor Start and its ambiguous delivery overlap with missing original history,
acknowledge the history omission first. The acknowledgement retains the unresolved command and
delivery for their separate reconciliation. Both steps require the existing verified zero hold.

## Roll back software

[Software rollback](release-process.md) concerns the proxy build and its journal while the charger stays on the
proxy route. Keep the state directory across upgrades. The rollback checker covers specific historical build
and journal combinations. The release rehearsal tests the candidate and its verified predecessor on a simulated
installation with the same journal.

## Restore direct vendor control

Use the restoration tool for an authorized removal of the managed proxy. It uses the original route, saved
current, and charger identity from the same private commissioning files. The loopback tests exercise a full
restoration and interrupted writes. Verify the installation's firmware and original vendor access with its
installer before using the procedure on the charger.

1. In Home Assistant, select 6 A while the charger is idle. Request **Pause charger** and wait for **Paused locally**.
   Check session closure and zero output, then unplug the car. Keep it unplugged until the tool reports completion.
2. Stop the proxy service and keep it stopped. Keep the charger powered and reachable. Back up the private
   journal, commissioning files and charger settings.
3. Run the offline check as the proxy service account. Run the explicit restore action from the same checkout.
   The offline check verifies the private native inputs. The restore validates the listener credentials before the
   stage that needs them.

```bash
python -B tools/restore_vendor.py check --config /etc/ics-proxy/proxy.json \
  --native-bounded-spec /etc/ics-proxy/native-spec.json
python -B tools/restore_vendor.py restore --config /etc/ics-proxy/proxy.json \
  --native-bounded-spec /etc/ics-proxy/native-spec.json
```

The tool verifies the original settings, closed session, and proxy-owned 0 A hold. It records that restoration
has begun before lowering the charger’s saved and effective limit to 6 A. A temporary listener uses the proxy’s
existing cleanup code to clear only its recorded profiles. It clears the station profile last and checks the
charger’s inventory before closing the listener. The tool restores the four saved route fields and verifies the
vendor connection before restoring and independently checking the original current limit.

If the tool stops partway through, keep the service stopped and the car unplugged. Retain the journal and rerun
the same restore action after correcting the reported problem. It uses the recorded stage and the charger’s
actual settings to pick up where it stopped. Once the tool has recorded profile cleanup, the remaining steps use
the charger’s local interface. If a cleared proxy profile reappears after cleanup is recorded and before the
route change, the tool stops. After the installer removes the reappeared profile, rerun the same restore action.
The tool verifies that the inventory matches the commissioned original resources and retains the recorded fault
in the journal. If the settings conflict with the commissioned record or the charger reports an open session,
resolve the issue with the installer before proceeding. When the tool reports
`complete: true`, keep the proxy retired and confirm direct vendor-app access before reconnecting the car.
