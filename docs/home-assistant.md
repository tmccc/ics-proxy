# Home Assistant client

Copy the integration to `custom_components/ics_proxy` inside your Home Assistant configuration directory.
In Home Assistant Container, the full destination is `/config/custom_components/ics_proxy`. You can also use the
[verified Docker installer](release-process.md) for a qualified source commit. Restart Home Assistant,
then add **iCS local proxy** in Devices & services. Enter the running proxy’s management port and token.
The client connects to `127.0.0.1`, and the form’s default port of `18779` matches the
[example management settings](../packaging/management.json.example).

The proxy binds the management WebSocket to loopback. In a container setup, use a topology in which the
Home Assistant client can reach that listener, such as the same host network. Keep the token private and
leave the management listener off public interfaces.

Home Assistant attaches the commissioned proxy marker from the status behind each control when the proxy supports it.
If another commissioned proxy takes over the same management address before the write arrives, it refuses the request.
Earlier proxies can still accept ordinary controls using the fields they support.

The entities show current and power, vehicle connection, session state, selected limit, control mode and
diagnostics. Start and the session-only Stop service use the same proxy as the vendor app. Pause holds at
zero. A charger can report a session after Pause if an earlier Start was already accepted. The proxy keeps
the zero hold, waits for the required charger evidence, and records and stops the exact session. A session can remain open
while charging power is zero. The proxy confirms closure from the charger’s closing report and fresh
independent evidence. Return gives control back to the app and charger schedule once the proxy can verify
the necessary conditions.

The `ics_proxy.session_command` action returns `confirmed: true` when the proxy completes the exact request.
See [the touchscreen example](../examples/solar-display-2/README.md) for the request format. Treat
`confirmed: false` as unconfirmed. That result covers several outcomes, including a Stop that never went out
and a Stop whose reply is still unknown. The session can show as closed while the proxy still waits for the
exact Stop reply. The saved result can settle after Home Assistant's wait ends.

Set `include_request_reference: true` and save the action response when you want to look up a specific Stop.

```yaml
- action: ics_proxy.session_command
  data:
    action: stop_session
    target: "{{ state_attr('binary_sensor.ics_proxy_session_open', 'stop_target') }}"
    include_request_reference: true
  response_variable: stop_result
```

Use `stop_result.request_reference` with `incident --kind stop --request-reference` on the proxy host. The
[Stop incident report](commissioning.md#run-the-service) uses the same value in `journal.request.request`, even
if a later Stop has arrived. A `null` action reference means Home Assistant refused the request locally. A
reference identifies an attempt, which may have been refused before the proxy saved it. If the report has no
matching request, check the charger’s current state before drawing conclusions about that attempt.

Before making a new Stop request after a failed attempt, check that the matching report has
`journal.request.state` set to `failed`, `wire.dispatch` set to `not_sent`, and the same session is still open.
A pending request can still send.
A Stop that may have gone out stays tied to its original attempt while the proxy looks for closure.
If an earlier Stop already covers the same open session when Home Assistant makes its request, the proxy joins
that attempt. The joined request follows that attempt's result regardless of when closure arrives.
Home Assistant Pause uses the protective path when the charger still needs to stop.

The client derives availability from fresh proxy data. A vendor outage and a local charger fault have
different meanings. The proxy can report a locally verified state while the vendor connection is down.

The Home Assistant diagnostics download records the client build when this Core process first sets up the
integration. The `installed_client` fields describe the files on disk when you download diagnostics. Those
fields can show a newer build after a file-only deployment. Missing build information appears as `null`.
