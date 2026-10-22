"""Exact Host-header protection for Klearing's loopback-only server.

Starlette's TrustedHostMiddleware deliberately removes the port before it
compares a Host header. Klearing has one local origin, so accepting the same
host name with an arbitrary claimed port is broader than the product's
localhost-only contract. This middleware permits exactly the two browser
origins Klearing opens itself and rejects duplicate Host headers as well.
"""

from starlette.responses import PlainTextResponse

from config import PORT


LOCAL_HOSTS = frozenset({f"localhost:{PORT}", f"127.0.0.1:{PORT}"})


class LocalHostMiddleware:
    """Allow HTTP and WebSocket requests only from Klearing's local origin."""

    def __init__(self, app, allowed_hosts=None):
        self.app = app
        self.allowed_hosts = frozenset(allowed_hosts or LOCAL_HOSTS)

    @staticmethod
    def _host_headers(scope):
        """Return every Host header exactly as the ASGI server received it."""
        return [value.decode("latin-1").lower()
                for name, value in scope.get("headers", [])
                if name.lower() == b"host"]

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        hosts = self._host_headers(scope)
        if len(hosts) == 1 and hosts[0] in self.allowed_hosts:
            await self.app(scope, receive, send)
            return

        if scope["type"] == "http":
            response = PlainTextResponse("Invalid host header", status_code=400)
            await response(scope, receive, send)
        else:
            await send({"type": "websocket.close", "code": 1008})
