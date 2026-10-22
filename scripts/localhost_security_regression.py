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
