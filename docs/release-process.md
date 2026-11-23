# Release and rollback

The development and hosted checks are useful for review. A live update also needs complete qualification tied
to the exact clean commit. The tools derive test collection from frozen source and verify every recorded
execution. Keep the raw evidence and executor configuration outside the repository.

There are three complete worker suites: `engine-model`, `ha-proxy` (Home Assistant 2026.11), and
`ha-proxy-2026.10`. Use `tools/parallel_qualify.py --help` for the required worker image, interpreter, output and
environment options. Use `tools/validate.py --help` to check a complete qualification bundle and its trusted
collection executors. The release target `ics-client` includes recovery discovery. A changed standalone
`ics-proxy` release also checks the pinned runtime and rehearses an installation week against the installed build.

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
and release checks on a copy before replacing the service. Stop and restart with the configured service manager.
Confirm the installed source fingerprint, current setting, state, vendor availability and preserved charging records.

The rollback checker accepts only exact historical build and journal pairs with rehearsed compatibility. It refuses
unsupported pairs. Preserve the verified installation and private journal backup across an update. A pending or
uncertain charger outcome must be reconciled before a rollback can safely use a previous journal.
