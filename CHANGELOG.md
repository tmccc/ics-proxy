# Changelog

## Unreleased

- Preserve exact Stop, Pause and closure evidence across delayed charger reports, temporary journal write
  failures and proxy replacement.
- Recheck charger evidence before restoring current or returning control to iCS. Preserve pending command
  outcomes and saved acknowledgements through native history repair.
- Keep Solar Display request feedback bound to the exact command across reconnects and reject stale controls.
  Home Assistant uses generic session-control fields and accepts earlier automation fields.
- Block new proxy-run overnight Starts until a charger-enforced cutoff is verified. Keep cutoff and recovery
  for existing scheduled occurrences.

## 0.3.0-rc1

- Rehearse rollback of exact standalone builds with a settled journal and a pre-upgrade backup.
- Identify locally authorized sessions in proxy history and preserve rejected Start evidence across replacement.
- Show the vendor restore stage, a reason code and the next action when restoration stops.
- Reserve a management connection for Pause and bind Home Assistant controls to the commissioned proxy reported
  in the status used for the request.
- Support opt-in owner Start while iCS is disconnected. Retain the locally authorized session and its charger
  reports in the proxy journal, including across a service restart.
- Let a Home Assistant session Stop join an unanswered Pause Stop for the same session when the charger is
  verified at zero. A queued vendor Stop can use the recorded replies for its original session after closure.
- Keep verified closure separate from the charger’s Stop reply. Save closure when the receipt write fails and
  settle pending receipts through graceful shutdown and restart. Reconcile unresolved Stop outcomes before
  using an older build with the journal. See [Release and rollback](docs/release-process.md).
- Use the same saved Stop reply checks in the proxy and incident reports. Settle exact saved Stop refusals
  and vendor acknowledgements during recovery from a matching charger-history fault.
- Require a live vendor connection for the current charger before sending a queued vendor Start or Return.
  Resume interrupted current recovery once the current connection and zero hold are verified again.
- Discard stale Solar Display 2 charger taps and wait for fresh Home Assistant feedback after reconnects or
  overlapping connections. The deployed display firmware was built with ESPHome 2026.10.1.
- Refuse hard-linked journal state files so each journal has one owner lock.
- Finish exact saved vendor acknowledgements during startup when they would block repair of missing charger history.
- Report the Home Assistant client build captured during setup and recover display feedback after an interrupted action.
- Check the linked OpenSSL libraries before reusing a passing release rehearsal.
- Read stopped, checkpointed journals from read-only backup directories in incident reports.
- Resume an interrupted return to the vendor backend once fresh readings confirm that a reappearing proxy
  profile has been removed, including when it reappeared before the first route check.
- Require evidence of the original session Stop before using a retained reply as acceptance.
- Build recovery coverage reports from the same frozen source and qualification results the validator checked.
- Join an earlier same-session Stop when accepting the local request and retain verified closure through
  temporary receipt write failures.
- Allow restricted restart after a known Stop during a vendor outage while the charger’s local status catches up.
- Disable Solar Display 2 charger buttons immediately when its Home Assistant automation is unavailable.
- Use fixed OpenSSL settings for offline release rehearsals and clarify the manual Home Assistant install path.
- Settle session Stop receipts consistently when closure and the reply arrive in either order. Show the accepted
  Stop attempt in reports for a request that joined it.
- Show Solar Display 2 request feedback between image refreshes and document the automatic first handover.
- Rehearse standalone upgrades with the installed and candidate dependency environments and track OpenSSL when
  reusing rehearsal results.
- Check elapsed evidence age before charger writes, including the commissioned 32 A write.
- Require current schedule evidence when a timed charging test expires.
- Clear an exact history-omission fault when all missing charger records return unchanged.
- Bound authorization refusal retries while preserving the order of accepted and refused decisions.
- Require fresh authorization after a restart interrupts the recording of a vendor decision.
- Save late vendor refusals and preserve their ordering when the approval cache fills.
- Recheck unplugged zero-output evidence at the point of sending vendor restoration writes.
- Check the Home Assistant version actually used by the installation rehearsal and reject editable dependencies.
- Isolate Python bytecode caches, check dependency contents before reusing release results, and verify SSH worker
  paths upfront.
- Expire stale Solar Display 2 images and accept fresh images after Home Assistant's clock moves backward.
- Add an optional Home Assistant Stop reference to look up the exact request in an incident report.
- Resume vendor restoration after profile cleanup without loading the retired listener credentials.
- Require an existing journal for operator repair and current preparation.
- Add a checked procedure to restore the original vendor route and current on an unplugged managed charger.
- List eligible fault reconciliation candidates with one read-only inspection.
- Fixed Solar Display 2 showing grey charger buttons when its Home Assistant touch automation was enabled.
- Keep damaged saved Start and Stop replies for diagnosis while recovering from valid evidence.
- Use stable recovery reasons for new current and history records while still reading older journals.
- Reject malformed authorization headers cleanly and document the network and electrical setup the proxy supports.
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
