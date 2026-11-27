# Release and rollback

The development and hosted checks are useful for review. A live update also needs complete qualification tied
to the exact clean commit. Full validation freezes the candidate before running static checks, then audits the
recorded worker executions against that same source. Keep the raw evidence and executor configuration outside
the repository.

There are three complete worker suites: `engine-model`, `ha-proxy` (Home Assistant 2026.11), and
`ha-proxy-2026.10`. Use `tools/parallel_qualify.py --help` for the required worker image, interpreter, output and
environment options. Use `tools/validate.py --help` to check a complete qualification bundle and its trusted
collection executors. SSH qualification requires absolute paths for the supervisor's `--worker-python` and
`--worker-root`. Both release targets include recovery discovery. The verifier uses the candidate's seeded
event orders and checks the recorded observations for each history. `ics-proxy` also checks the standalone
runtime pins in the engine worker and in the Home Assistant interpreter that runs the standalone service during
the installation week. The rehearsal verifies the version it imports against the candidate's Home Assistant pins.
A change to the standalone service, its dependency pins, or its packaged systemd unit triggers that rehearsal
against the installed build.

Pass `--ics-ha-python /path/to/ha-env/bin/python` to run the installation week. When the installed and
candidate `requirements-trial.txt` pins differ, also pass `--ics-installed-python /path/to/old-env/bin/python`
with the verified installed build's standalone dependencies. The same option can select a distinct runtime
when the pins match. With matching pins and no separate interpreter, the service processes share the Home
Assistant environment. The simulator imports the archived build's commissioning code in the Home Assistant
environment. Those imports must work with the candidate dependencies. The service processes use the installed
runtime before the upgrade and after rollback, and the candidate runtime during the upgrade.

Both checks use a fresh Python cache location and set `OPENSSL_CONF=/dev/null` for synthetic certificates,
including in the simulated service processes. They clear inherited `OPENSSL_*` overrides. Reusing a passing
result requires matching test evidence, interpreter and dependency files, OpenSSL executables, and rehearsal
configuration. The checks also fingerprint the OpenSSL libraries resolved for the fixture and service commands
and loaded by the rehearsal interpreters. If the library inventory is unavailable, the check runs again. The
installation week checks both assigned runtimes. Updating a package or a linked library in place invalidates
the result even when its version stays the same. Use installed packages in the interpreter's standard
library and package directories. The release checks fingerprint individual root-managed Python startup and
shared-library files at their distribution link targets. The checks reject editable installs and other import
roots without walking external source trees.

Before creating a release tag, move the changes from `Unreleased` into the matching version section of
`CHANGELOG.md`. Run `python tools/version.py check vX.Y.Z` with the intended tag.

A previously installed commit from the upstream layout may be present only in the original checkout.
For a cross-repository rehearsal, import that exact verified commit into your local checkout with
`git fetch /path/to/original/checkout <full-installed-commit>`. Supply it as the installed commit to validation.
The rehearsal recognizes the installed layout and commissions from the old source before replacing it with
the candidate. Keep any local historical references separate from the published branch.

## Install the Home Assistant client

```bash
python tools/deploy_ha.py --container "$HA_CONTAINER" --dry-run
OCPP_QUALIFICATION_REPORT="$QUALIFICATION_BUNDLE" python tools/deploy_ha.py \
  --container "$HA_CONTAINER" --batch --collection-executors "$TRUSTED_EXECUTORS"
```

The Docker installer checks the candidate, backs up the previous files, and verifies the installed bytes. An
authorized restart can be included with `--restart --yes --ha-api-config "$HA_API_CONFIG"`. The private settings
contain `token_file` and can include `container_port`. The process lock spans configuration validation and restart
verification. This tool installs the Home Assistant client. The standalone service has its own release procedure.

## Update the proxy

Use a clean qualified source, a private state backup, and an authorized idle window. Rehearse journal initialization
and release checks on a copy before replacing the service.

The installation week starts the Python service directly. For a systemd deployment, stage the adapted
[service unit](../packaging/ics-proxy.service.example) as an `ics-proxy.service` file and verify it with
`systemd-analyze verify /path/to/staged/ics-proxy.service` on the target host. Check the unit's execution paths,
service account, writable state directory, watchdog and stop timeout. The example spaces restart attempts three
seconds apart so a brief charger outage can recover under the default systemd start limits. Check the spacing
against any limits configured on the target host. Run its `managed_proxy.py check` invocation
against the staged source and corresponding private configuration with the configured service account and its
deployment environment, including its OpenSSL settings.

Stop and restart with the configured service manager in the authorized window.
Confirm the installed source fingerprint, current setting, state, vendor availability and preserved charging records.

The stopped-journal rollback checker accepts only exact historical build and journal pairs with rehearsed
compatibility. For a standalone candidate, rehearse the exact installed and candidate source with synthetic
charger state before the update.

```bash
python tools/rehearse_proxy_rollback.py --installed-proxy-commit "$INSTALLED_COMMIT" \
  --ics-ha-python "$HA_PYTHON" --output "$PRIVATE_EVIDENCE"
```

The rehearsal includes a settled-journal rollback and a rollback using a paused predecessor's backup.
For a newer standalone installation, use the rehearsal to prepare the backup rollback.

1. Before the update, archive the exact predecessor source and its runtime. Take a consistent private SQLite
   backup of the stopped predecessor journal after verifying a Pause, closed session and zero output.
   Preserve committed WAL content in any raw archive. Use a checkpointed copy for the verifier below.
2. If rollback is needed, retain the stopped candidate journal separately and match the archived predecessor
   to the installed commit in the rehearsal. Verify current charger closure and zero output. Compare any
   post-update sessions, receipts and device changes with the pre-update state.
3. Restore a copy of the pre-update backup only after reconciling intervening activity. If the charger state
   or any intervening outcome remains uncertain, keep the charger paused and use the current build's
   recovery procedure.

Verify the saved state of a checkpointed predecessor backup before considering it for restoration.

```bash
python tools/check_proxy_rollback.py --config "$PRIVATE_CONFIG" --pre-update-backup "$PRE_UPDATE_BACKUP"
```

A `backup_verified` result covers the backup's saved Pause, shutdown and settled lifecycle. Check the
exact-build rehearsal and current charger state separately. The historical checker remains the authority
for the specific older in-place rollback pairs it recognizes.

Builds from before stable recovery reasons can reject a newer failure record for an unsettled current change.
Reconcile that change with the build that recorded it before considering a downgrade. The installation
rehearsal covers a settled journal and does not establish downgrade compatibility for every failure state.
Resolve interrupted vendor authorization checks on the build that recorded them before considering a downgrade.

## Qualify the overnight outage schedule

Production builds block new automatic outage Starts until the commissioned firmware can end a proxy-started
session independently. The prototype holds zero while opening a session, binds the charger's actual
transaction number, and records an accepted charger-side timer before raising the ceiling. If the exact
timer reply or the journal write is missing, it keeps the zero hold. A replacement connection must
stop the exact session before offering it positive current. The native timer can end charger-owned
sessions. A proxy-issued Stop at 06:00 verifies cutoff with the proxy available. For the outage path,
use independent charger readings while the proxy is unavailable.

Qualify a candidate at the agreed site current. Check an unsynchronized start after host power loss, loss of
synchronization before Start, and an in-window vendor outage. For the cutoff case, retain evidence that the
proxy started the session, that the vehicle still requested current before the deadline, and that the proxy was
unavailable across 06:00. Confirm from charger-side readings that the exact session ended and output reached
zero before the proxy returned. Verify that bringing the proxy back does not start another session in the same
window. Retain the redacted evidence with the approved source commit and firmware version.
