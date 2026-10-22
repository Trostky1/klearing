"""Offline contract coverage for the SimpleFIN adapter's own logic (token
exchange, account/transaction normalization). Registration and Setup wizard
step-3 branching are covered separately in
scripts/simplefin_registration_regression.py."""

import base64
import contextlib
import io
import sys
from datetime import date, datetime, time, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import banks  # noqa: E402
from banks import simplefin  # noqa: E402


checks = 0
failures = 0


def check(label, condition):
    global checks, failures
    checks += 1
    if condition:
        print(f"  PASS  {label}")
    else:
        failures += 1
        print(f"  FAIL  {label}")


class Response:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def epoch(day):
    return int(datetime.combine(day, time(hour=12),
                                tzinfo=timezone.utc).timestamp())


def run():
    adapter = simplefin.SimpleFINAdapter()
    original_post = simplefin.requests.post
    original_get = simplefin.requests.get
    calls = []
    try:
        print("Setup-token exchange")
        claim_url = "https://bridge.simplefin.test/simplefin/claim/once"
        token = base64.b64encode(claim_url.encode()).decode()
        access_url = "https://user:p%40ss@api.simplefin.test/simplefin"
        simplefin.requests.post = lambda url, timeout: (
            calls.append(("post", url, timeout))
            or Response(text=access_url))
        check("one-use setup token decodes and exchanges over HTTPS",
              simplefin.exchange_setup_token(token) == access_url
              and calls[-1][1] == claim_url)
        insecure = base64.b64encode(
            b"http://bridge.simplefin.test/simplefin/claim/once").decode()
        try:
            simplefin.exchange_setup_token(insecure)
        except simplefin.SimpleFINError as exc:
            insecure_rejected = "HTTPS" in str(exc)
        else:
            insecure_rejected = False
        check("non-HTTPS claim URLs are rejected before a request",
              insecure_rejected and len(calls) == 1)
        simplefin.requests.post = lambda *_args, **_kwargs: Response(status=403)
        try:
            simplefin.exchange_setup_token(token)
        except simplefin.SimpleFINError as exc:
            claimed_rejected = "disable it" in str(exc) and "fresh token" in str(exc)
        else:
            claimed_rejected = False
        check("claimed token failure gives the compromise-safe recovery",
              claimed_rejected)

        print("Accounts and authentication")
        today = date.today()
        payload = {
            "errors": ["Requested date range exceeds 90 days and was capped."],
            "errlist": [{"code": "act.missingdata",
                         "msg": "One institution supplied partial descriptors."}],
            "accounts": [
                {"id": "checking", "name": "Checking", "currency": "USD",
                 "balance": "240.14", "transactions": [
                    {"id": "same-id", "posted": epoch(today), "amount": "0.14",
                     "description": "Mercury Cashback"},
                    {"id": "card-1", "posted": epoch(today), "amount": "-31.50",
                     "description": "Card payment"},
                    {"id": "pending", "posted": 0, "amount": "-10.00",
                     "description": "Pending", "pending": True},
                 ]},
                {"id": "reserve", "name": "Reserve", "currency": "USD",
                 "balance": "50", "transactions": [
                    {"id": "same-id", "posted": epoch(today), "amount": "5.00",
                     "description": "Transfer"},
                 ]},
                {"id": "cad", "name": "Canadian", "currency": "CAD",
                 "balance": "20", "transactions": [
                    {"id": "cad-1", "posted": epoch(today), "amount": "2",
                     "description": "CAD deposit"},
                 ]},
            ],
        }

        def fake_get(url, auth, params, timeout):
            calls.append(("get", url, auth, dict(params), timeout))
            return Response(payload=payload)

        simplefin.requests.get = fake_get
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            transactions = adapter.fetch_transactions(
                {"access_url": access_url}, since_date="2000-01-01")
            accounts = adapter.list_accounts({"access_url": access_url})
        get_calls = [call for call in calls if call[0] == "get"]
        check("Basic Auth is explicit and absent from the request URL",
              all("@" not in call[1] and call[2] == ("user", "p@ss")
                  for call in get_calls))
        earliest = date.today().toordinal() - (simplefin.MAX_HISTORY_DAYS - 1)
        start_day = datetime.fromtimestamp(
            get_calls[0][3]["start-date"], tz=timezone.utc).date().toordinal()
        check("every request carries a start-date clamped to 90 days",
              all("start-date" in call[3] for call in get_calls)
              and start_day == earliest)
        check("balance-only calls still carry start-date",
              get_calls[-1][3].get("balances-only") == 1
              and "start-date" in get_calls[-1][3])
        by_id = {row["external_id"]: row for row in transactions}
        check("SimpleFIN signs pass through unchanged",
              by_id["simplefin:checking:same-id"]["amount"] == 0.14
              and by_id["simplefin:checking:card-1"]["amount"] == -31.5)
        check("transaction IDs are namespaced by account",
              "simplefin:checking:same-id" in by_id
              and "simplefin:reserve:same-id" in by_id)
        check("pending and non-USD transactions are not imported",
              len(transactions) == 3 and len(accounts) == 2)
        check("valid data survives soft protocol warnings",
              "SimpleFIN warning:" in output.getvalue()
              and "partial descriptors" in output.getvalue())
        check("adapter is registered in the built-in registry",
              "simplefin" in {item.id for item in banks.all_adapters()}
              and isinstance(banks.get_adapter("simplefin"),
                             simplefin.SimpleFINAdapter))

        print("Connection result")
        result = adapter.test_connection({"access_url": access_url})
        check("connection test reports usable USD accounts",
              result.ok and "2 USD account(s)" in result.message)
    finally:
        simplefin.requests.post = original_post
        simplefin.requests.get = original_get

    if failures:
        print(f"FAIL: {failures} of {checks} SimpleFIN adapter checks failed.")
        return 1
    print(f"PASS: {checks}/{checks} SimpleFIN adapter checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
