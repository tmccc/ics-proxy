"""WebSocket connections bound to the configured vendor address."""

from websockets.asyncio.client import connect


class FixedVendorConnect(connect):  # type: ignore[misc]
    """A credential-bearing vendor connection never follows an HTTP redirect."""

    # Return the original error, never a replacement URI that could receive authentication.
    def process_redirect(self, exc: Exception) -> Exception | str:
        return exc
