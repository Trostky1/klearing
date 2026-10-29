"""Regression coverage for Klearing's localhost-only release boundary.

Runs a real loopback Uvicorn server rather than relying on a test client, so
the ASGI Host header is exercised as the packaged app receives it. No network
request leaves this machine.
"""

import http.client
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path


PORT = 18643
_DATA = tempfile.TemporaryDirectory(
    prefix="klearing-localhost-security-", ignore_cleanup_errors=True)
os.environ["KLEARING_DATA_DIR"] = _DATA.name
os.environ["KLEARING_PORT"] = str(PORT)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import app  # noqa: E402
import local_host  # noqa: E402
import main  # noqa: E402
import uvicorn  # noqa: E402


def check(label, condition, detail=""):
    if condition:
        print(f"PASS: {label}")
        return 0
    print(f"FAIL: {label}" + (f" — {detail}" if detail else ""))
    return 1


def wait_for_port(timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if probe.connect_ex(("127.0.0.1", PORT)) == 0:
                return True
        time.sleep(0.05)
    return False


def request(*hosts):
    connection = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
    connection.putrequest("GET", "/", skip_host=True)
    for host in hosts:
        connection.putheader("Host", host)
    connection.endheaders()
    response = connection.getresponse()
    response.read()
    connection.close()
    return response.status


def browser_request(method, path, **headers):
    """One request with the given extra headers; returns (status, headers)."""
    connection = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
    connection.putrequest(method, path, skip_host=True)
    connection.putheader("Host", f"localhost:{PORT}")
    connection.putheader("Content-Length", "0")
    for name, value in headers.items():
        connection.putheader(name.replace("_", "-"), value)
    connection.endheaders()
    response = connection.getresponse()
    response.read()
    result = (response.status, {k.lower(): v for k, v in response.getheaders()})
    connection.close()
    return result


def main_regression():
    failures = 0
    failures += check("both server modes bind only loopback",
                      main.LOCAL_BIND_HOST == "127.0.0.1")
    failures += check("allowed Host set is exact for the configured port",
                      local_host.LOCAL_HOSTS == {
                          f"localhost:{PORT}", f"127.0.0.1:{PORT}"})

    server = uvicorn.Server(uvicorn.Config(
        app.app, host=main.LOCAL_BIND_HOST, port=PORT,
        log_level="critical", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        failures += check("real loopback server starts", wait_for_port())
        if failures:
            return 1
        failures += check("localhost with the configured port is accepted",
                          request(f"localhost:{PORT}") != 400)
        failures += check("numeric loopback with the configured port is accepted",
                          request(f"127.0.0.1:{PORT}") != 400)
        failures += check("foreign Host header is rejected", request("evil.test") == 400)
        failures += check("localhost without the configured port is rejected",
                          request("localhost") == 400)
        failures += check("localhost with a wrong port is rejected",
                          request("localhost:9999") == 400)
        failures += check("duplicate Host headers are rejected",
                          request(f"localhost:{PORT}", "evil.test") == 400)

        # Cross-site request guard: a page on another site can make a browser
        # POST to localhost, and the browser labels that request.
        evil = browser_request("POST", "/review/deny", Origin="http://evil.test")[0]
        failures += check("a POST with a foreign Origin is refused", evil == 403,
                          str(evil))
        cross = browser_request("POST", "/review/deny",
                                Sec_Fetch_Site="cross-site")[0]
        failures += check("a POST marked cross-site by the browser is refused",
                          cross == 403, str(cross))
        for origin in (f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}"):
            own = browser_request("POST", "/review/deny", Origin=origin)[0]
            failures += check(f"a POST from Klearing's own origin {origin} is not refused",
                              own != 403, str(own))
        bare = browser_request("POST", "/review/deny")[0]
        failures += check("a POST with no Origin (script, curl) is not refused",
                          bare != 403, str(bare))
        evil_get = browser_request("GET", "/", Origin="http://evil.test")[0]
        failures += check("a GET is not blocked by the cross-site guard",
                          evil_get != 403, str(evil_get))
        refused = browser_request("POST", "/review/deny", Origin="http://evil.test")[1]
        failures += check("a refused request still carries security headers",
                          refused.get("x-frame-options") == "DENY")
        _, headers = browser_request("GET", "/")
        failures += check("pages carry nosniff, no-frame, and no-referrer headers",
                          headers.get("x-content-type-options") == "nosniff"
                          and headers.get("x-frame-options") == "DENY"
                          and headers.get("referrer-policy") == "no-referrer"
                          and "frame-ancestors 'none'" in headers.get(
                              "content-security-policy", ""))
    finally:
        server.should_exit = True
        thread.join(timeout=10)

    if failures:
        print(f"FAIL: {failures} localhost security checks failed.")
        return 1
    print("PASS: localhost security checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_regression())
