# Changelog

## Unreleased

- Added read-only incident reports for local and vendor Stops and included saved backup replies in Start reports.
- Fixed a timing issue that could prevent Pause from recording and stopping a session the charger reported late.
- Rechecked current charger evidence before sending a session-only Stop and preserved pending Pause through history repair.
- Preserved Stop confirmation through a temporary receipt write failure and supported retrying the first listener bind.
- Kept supervision running when a Stop timeout or rejection receipt write rolls back and used elapsed time for Stop
  confirmation and native evidence freshness.
- Confirmed a queued Stop from the exact original session's verified closure and made the shutdown hold claim atomic.
- Preserved existing fault and shutdown status when the listener certificate is nearing expiry.
- Checked the standalone dependency pins during the installation rehearsal and documented the first charger handover.
- Kept release tags from omitting pending changelog entries and spaced proxy restarts during temporary charger outages.
- Linked coverage reports to the qualification files actually audited when the command runs from another directory.
- Retired expired native readings and earlier Boot samples from live status and charger request admission.
- Kept exact Stop rejections authoritative while their wire replies await storage.
- Settled saved Start and Stop replies against the original attempt during normal operation and recovery.
- Prevented an expired session Stop from being sent while its timeout update awaits storage.
- Shared an unanswered Stop between Pause and the vendor app while the charger is verified at zero.
- Let a definite rejection release a shutdown wait tied to the original ordinary Stop request.
- Retried the first charger introduction after a temporary journal read failure.
- Hid Solar Display 2 charger buttons while their Home Assistant automation is unavailable.
- Included the service tools package in the installed source fingerprint and enforced the hosted release check deadline.
- Used Stop time to identify the latest completed charge in native history and older Home Assistant status responses.
- Used the later Start to break a Stop time tie and left conflicting energy readings with identical times unavailable.
- Required Home Assistant to confirm the proxy is running and to verify a local hold explicitly before showing it.
- Preserved the shutdown grace period for test workers and reported hard failures even when the last worker exits.
- Recorded current meter activity when a charger report contains a fresh sample after older samples.
- Resolved known Start rejections under a verified Pause and recorded whether shutdown reached the Stop send boundary.
- Kept Home Assistant’s management requests on loopback and preserved Start and Stop after a failed unload.
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
