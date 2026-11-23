# Home Assistant client

Copy `custom_components/ics_proxy` into the Home Assistant configuration directory, or use the
[verified Docker installer](release-process.md) for a qualified source commit. Restart Home Assistant,
then add **iCS local proxy** in Devices & services. Enter the management host, port and token for the already
running proxy.

The proxy binds the management WebSocket to loopback. In a container setup, use a topology in which the
Home Assistant client can reach that listener, such as the same host network. Keep the token private and
leave the management listener off public interfaces.

The entities show current and power, vehicle connection, session state, selected limit, control mode and
diagnostics. Start and the session-only Stop service use the same proxy as the vendor app. Pause holds at
zero. Return gives control back to the app and charger schedule once the proxy can verify the necessary
conditions. A session can remain open while charging power is zero. The proxy confirms closure from the
charger’s closing report and fresh independent evidence.

The `ics_proxy.session_command` action returns a response for the exact Start or Stop request. See
[the touchscreen example](../examples/solar-display-2/README.md) for the request format. An unclear Stop
outcome stays tied to its original attempt. Home Assistant Pause uses the protective path when the charger
still needs to stop.

The client derives availability from fresh proxy data. A vendor outage and a local charger fault have
different meanings. The proxy can report a locally verified state while the vendor connection is down.
