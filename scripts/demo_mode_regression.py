"""Regression coverage for demo mode ("Take a look").

A demo opens a made-up business in its own folder. These checks prove the
real data folder is never touched, the demo is complete and balanced, the
banner and the Setup button are there, and routes that reach a real service
are turned off. A real loopback server is used for the route checks; no
request leaves this machine.
"""

import http.client
import os
import socket
import sys
import tempfile
import threading
import time
from decimal import Decimal
from pathlib import Path

PORT = 18644
_DATA = tempfile.TemporaryDirectory(
    prefix="klearing-demo-", ignore_cleanup_errors=True)
os.environ["KLEARING_DATA_DIR"] = _DATA.name
os.environ["KLEARING_PORT"] = str(PORT)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import app  # noqa: E402
import db  # noqa: E402
import demo_mode  # noqa: E402
import ledger  # noqa: E402
import main  # noqa: E402
import paths  # noqa: E402
import secrets_store  # noqa: E402
import uvicorn  # noqa: E402


def check(label, condition, detail=""):
    if condition:
        print(f"PASS: {label}")
        return 0
    print(f"FAIL: {label}" + (f" - {detail}" if detail else ""))
    return 1


def wait_for_port(timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if probe.connect_ex(("127.0.0.1", PORT)) == 0:
                return True
        time.sleep(0.05)
    return False


def call(method, path):
    connection = http.client.HTTPConnection("127.0.0.1", PORT, timeout=20)
    connection.putrequest(method, path, skip_host=True)
    connection.putheader("Host", f"localhost:{PORT}")
    connection.putheader("Content-Length", "0")
    connection.endheaders()
    response = connection.getresponse()
    body = response.read().decode("utf-8", "replace")
    result = (response.status, response.getheader("location") or "", body)
    connection.close()
    return result


def run():
    failures = 0
    real_db = paths.db_path()

    # --- The module itself
    failures += check("a fresh install is not in demo mode", not paths.is_demo())
    demo_mode.activate()
    failures += check("activating switches to the demo folder",
                      paths.is_demo()
                      and paths.data_dir() == demo_mode.demo_dir()
                      and paths.db_path() != real_db)
    failures += check("the demo counts as finished setup",
                      db.get_setting("setup_complete") == "1")
    failures += check("the demo has a full set of contacts", len(db.list_contacts()) == 15)
    invoices = db.list_invoices()
    failures += check("the demo has invoices", len(invoices) == 5, str(len(invoices)))
    failures += check("the demo has transactions waiting for review",
                      len(db.list_items(status="pending")) == 8)
    failures += check("the demo has approved transactions",
                      len(db.list_items(status="approved")) >= 40)
    rows = ledger.trial_balance()
    debit = sum((Decimal(str(r["debit_total"])) for r in rows), Decimal("0"))
    credit = sum((Decimal(str(r["credit_total"])) for r in rows), Decimal("0"))
    failures += check("the demo ledger balances", debit == credit and debit > 0,
                      f"{debit} vs {credit}")

    books = app.gather_reconciliation()
    failures += check("the demo's bank balance reconciles with its books",
                      books["flagged"] is False, str(books["residual"]))

    page = app.render_home_page()
    page = getattr(page, "body", page)
    page = page.decode() if isinstance(page, bytes) else page
    failures += check("pages show the demo banner",
                      "made-up data" in page and "/demo/exit" in page)

    try:
        secrets_store.set_secret("demo-test", "x")
        stored = True
    except secrets_store.SecretsError:
        stored = False
    failures += check("a demo never stores credentials", not stored)

    demo_mode.deactivate()
    failures += check("leaving the demo returns to the real folder",
                      not paths.is_demo() and paths.db_path() == real_db)
    db.init_db()
    failures += check("real data was never touched",
                      db.list_contacts() == [] and db.list_invoices() == [])

    demo_mode.activate()
    db.create_contact("Added during the demo")
    demo_mode.activate()
    names = [c["display_name"] for c in db.list_contacts()]
    failures += check("each visit starts a fresh demo",
                      "Added during the demo" not in names and len(names) == 15)
    demo_mode.deactivate()

    # --- Over HTTP
    server = uvicorn.Server(uvicorn.Config(
        app.app, host=main.LOCAL_BIND_HOST, port=PORT,
        log_level="critical", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        if check("real loopback server starts", wait_for_port()):
            return 1
        status, _, body = call("GET", "/setup/step/1")
        failures += check("the first Setup screen has a Take a look button",
                          status == 200 and "Take a look" in body
                          and 'action="/setup/demo"' in body, str(status))
        status, location, _ = call("POST", "/setup/demo")
        failures += check("Take a look opens the demo", status == 303
                          and location == "/", f"{status} {location}")
        status, _, body = call("GET", "/")
        failures += check("Home opens in the demo with a banner",
                          status == 200 and "made-up data" in body, str(status))
        status, location, _ = call("POST", "/check")
        failures += check("a route that reaches a real service is off in the demo",
                          status == 303 and "not%20connected" in location,
                          f"{status} {location}")
        status, location, _ = call("POST", "/demo/exit")
        failures += check("Exit demo leaves the demo", status == 303, str(status))
        status, location, _ = call("GET", "/")
        failures += check("after exit the real, unfinished setup is back",
                          status == 303 and location.startswith("/setup"),
                          f"{status} {location}")
    finally:
        server.should_exit = True
        thread.join(timeout=10)

    if failures:
        print(f"FAIL: {failures} demo mode checks failed.")
        return 1
    print("PASS: demo mode checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
