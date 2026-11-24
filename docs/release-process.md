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

Both checks use a fresh Python cache location, including the simulated service processes they start. Reusing a
passing result requires matching test evidence and the same interpreter and dependency files. Updating a package
in place invalidates the previous result even when its version stays the same. Use installed packages in the
interpreter's standard library and package directories. The release checks reject editable installs and other import
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
against the staged source and corresponding private configuration as the configured service account.

Stop and restart with the configured service manager in the authorized window.
Confirm the installed source fingerprint, current setting, state, vendor availability and preserved charging records.

The rollback checker accepts only exact historical build and journal pairs with rehearsed compatibility. It refuses
unsupported pairs. Preserve the verified installation and private journal backup across an update. A pending or
uncertain charger outcome must be reconciled before a rollback can safely use a previous journal.

Builds from before stable recovery reasons can reject a newer failure record for an unsettled current change.
Reconcile that change with the build that recorded it before considering a downgrade. The installation
rehearsal covers a settled journal and does not establish downgrade compatibility for every failure state.
Resolve interrupted vendor authorization checks on the build that recorded them before considering a downgrade.
