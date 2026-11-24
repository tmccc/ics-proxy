# Reporting a security issue

Use GitHub’s private vulnerability reporting for this repository when available. Keep charger serial numbers,
installation routes, device credentials, Home Assistant tokens and journals out of public reports.

## Network setup

The proxy reads the charger’s native LAN API over plain HTTP with Basic authentication. Keep native API traffic
on a trusted, isolated local network segment.

The separate OCPP connections from the charger to the proxy and from the proxy to the vendor use TLS. With
`incoming_auth: none`, the charger listener relies on the configured source address and WebSocket path for
admission. With `incoming_auth: basic`, it also checks the charger’s Basic credential. Keep access to the listener
limited to the charger’s network. The management WebSocket uses loopback and a separate token.
