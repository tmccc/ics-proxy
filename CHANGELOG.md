# Changelog

## Unreleased

- Fixed a timing issue that could prevent Pause from recording and stopping a session the charger reported late.
- Rechecked current charger evidence before sending a session-only Stop and preserved pending Pause through history repair.
- Kept Home Assistant’s management requests on the configured loopback connection by rejecting redirects.
- Extracted the active iCS engine, Home Assistant client, tests, deployment tools and documentation into a standalone repository.
- Preserved one proxy owner for charging actions and retained the existing control and journal formats.
- Kept queued charger reports for normal delivery during verified history repair, retried temporary native read failures,
  and preserved earlier physical refusals if an intervening reading is missing.
- Corrected immediate vendor profile Clear, bounded routine rejection records, and kept status reporting alive after
  rendering errors.
- Added a safe retry interval when vendor registration is deferred and fixed client outage notices after re-enable.
- Added a portable display example with exact request bindings, pending feedback, and expiry of old failure messages.

## 0.2.3

- Base version carried forward from the upstream iCS proxy.
