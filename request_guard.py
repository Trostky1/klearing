"""Cross-site request guard and baseline security headers.

Klearing listens on loopback only, and local_host.py rejects any request whose
Host header is not its own origin. That stops DNS rebinding, but it does not
stop a page on another website from making the visitor's browser send a form
POST to http://localhost:8642. Browsers attach an Origin header (and a
Sec-Fetch-Site header) to those requests, so this guard refuses a state
changing request whose Origin is not Klearing's own.

Requests with no Origin and no Sec-Fetch-Site header (scripts, curl, the test
client) are allowed: only a browser can be tricked into sending a cross-site
request, and a browser always labels one.
"""

from config import PORT


UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

ALLOWED_ORIGINS = frozenset({
    f"http://localhost:{PORT}",
    f"http://127.0.0.1:{PORT}",
})

# A conservative policy: it only forbids framing, base-tag tricks and plugins.
# Klearing's pages use inline scripts and styles and load Plaid Link from its
# CDN, so a full script policy would break the app until those are reworked.
CONTENT_SECURITY_POLICY = (
    "frame-ancestors 'none'; base-uri 'self'; object-src 'none'")

SECURITY_HEADERS = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
    (b"content-security-policy", CONTENT_SECURITY_POLICY.encode("latin-1")),
)


def _header(scope, name):
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def is_cross_site(scope):
    """True when a browser says this request came from somewhere else.

    Sec-Fetch-Site is set by the browser and a web page cannot change it, so
    it decides when it is present: only "same-origin" (and "none", a request
    the user typed) pass. A page on another port of localhost is "same-site",
    not "same-origin", and is refused. Older browsers that do not send it fall
    back to the Origin header. Origin alone is not enough, because a browser
    sends "Origin: null" on a same-origin form post when the page's referrer
    policy is "no-referrer".
    """
    fetch_site = _header(scope, b"sec-fetch-site")
    if fetch_site is not None:
        return fetch_site.lower() not in ("same-origin", "none")
    origin = _header(scope, b"origin")
    return origin is not None and origin.lower() not in ALLOWED_ORIGINS


class RequestGuardMiddleware:
    """Refuse cross-site state changes and add security headers to responses."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if scope["method"] in UNSAFE_METHODS and is_cross_site(scope):
            body = b"Cross-site request refused"
            await send({
                "type": "http.response.start", "status": 403,
                "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                            (b"content-length", str(len(body)).encode()),
                            *SECURITY_HEADERS],
            })
            await send({"type": "http.response.body", "body": body})
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                present = {key.lower() for key, _ in message.get("headers", [])}
                message = dict(message)
                message["headers"] = list(message.get("headers", [])) + [
                    header for header in SECURITY_HEADERS
                    if header[0] not in present]
            await send(message)

        await self.app(scope, receive, send_with_headers)
