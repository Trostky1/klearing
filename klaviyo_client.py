"""
Klaviyo API client (fixed, built-in integration — optional to connect).

Connecting Klaviyo unlocks the Follow-ups tab: Klearing watches one
Klaviyo list for new leads and drafts follow-up emails for the ones
who never got a reply. Read-only against Klaviyo.

The private API key lives in the secrets store; the list id (not a
secret) lives in settings.
"""

import requests

import connectivity
import db
import secrets_store

KLAVIYO_API_BASE = "https://a.klaviyo.com/api"
KLAVIYO_REVISION = "2024-10-15"

PRIVATE_KEY_SECRET = "klaviyo.private_api_key"
LIST_ID_KEY = "klaviyo_list_id"

KEY_PREFIX = "pk_"


def load_config():
    """(api_key, list_id) or None when Klaviyo isn't connected."""
    db.init_db()
    api_key = secrets_store.get_secret(PRIVATE_KEY_SECRET)
    list_id = (db.get_setting(LIST_ID_KEY) or "").strip()
    if not api_key or not list_id:
        return None
    return api_key, list_id


def is_configured():
    return load_config() is not None


def validate_key_format(api_key):
    """None if it looks like a private key; otherwise a specific error.
    Never makes a network call."""
    api_key = (api_key or "").strip()
    if not api_key:
        return "Klaviyo private key is empty. Paste it in (don't retype it)."
    if not api_key.startswith(KEY_PREFIX):
        if len(api_key) <= 8 and api_key.isalnum():
            return (
                "That looks like your 6-character public site ID, not a "
                'private key. Private keys start with "pk_" — create one '
                "in Klaviyo under Settings → Account → API keys."
            )
        return (
            f'A Klaviyo private key starts with "{KEY_PREFIX}" — yours '
            f'starts with "{api_key[:4]}". The beginning was probably '
            f"lost in the copy; copy the whole key again."
        )
    if len(api_key) < 20:
        return (
            f"The key looks too short ({len(api_key)} characters; "
            f"expected roughly 37). Part of it was probably cut off in "
            f"the paste — copy it again in one go."
        )
    return None


def _headers(api_key):
    return {
        "Authorization": f"Klaviyo-API-Key {api_key}",
        "accept": "application/json",
        "revision": KLAVIYO_REVISION,
    }


def _get(api_key, url):
    response = requests.get(url, headers=_headers(api_key), timeout=20)
    connectivity.raise_for_rate_limit("Klaviyo", response.status_code)
    if response.status_code != 200:
        raise KlaviyoHTTPError(response.status_code, response.text)
    return response.json()


class KlaviyoHTTPError(RuntimeError):
    def __init__(self, status_code, body):
        self.status_code = status_code
        super().__init__(f"Klaviyo returned HTTP {status_code}: {body}")


def list_lists(api_key):
    """[{'id': ..., 'name': ...}] for the account's lists. Follows
    cursor pagination; unlike the profiles endpoint, /api/lists/
    rejects a page[size] parameter, so none is sent."""
    payload = _get(api_key, f"{KLAVIYO_API_BASE}/lists/")
    return [
        {"id": item.get("id"),
         "name": (item.get("attributes") or {}).get("name", "Unnamed list")}
        for item in payload.get("data", [])
    ]


def fetch_list_profiles(api_key, list_id):
    """
    All profiles on the configured list, with their properties.
    Follows Klaviyo's cursor pagination.
    """
    profiles = []
    url = f"{KLAVIYO_API_BASE}/lists/{list_id}/profiles/?page[size]=100"
    while url:
        payload = _get(api_key, url)
        profiles.extend(payload.get("data", []))
        url = (payload.get("links") or {}).get("next")
    return profiles


def test_connection(api_key, list_id=None):
    """Live check for the Setup wizard. Verifies the key, and the list
    id too when one is given. Returns connectivity.TestResult; on
    success, result.lists carries the account's lists for a picker."""
    try:
        lists = list_lists(api_key)
    except connectivity.RateLimitError as exc:
        return connectivity.fail(str(exc))
    except KlaviyoHTTPError as exc:
        if exc.status_code == 401:
            return connectivity.fail(
                "Klaviyo rejected the key (HTTP 401). It may have been "
                "revoked, or a character changed in the copy — create a "
                "fresh private key and paste it again."
            )
        if exc.status_code == 403:
            return connectivity.fail(
                "The key is real, but it doesn't have the Lists "
                "read scope. In Klaviyo, create a key with Read access "
                "to Lists (a Full Access private key also works)."
            )
        return connectivity.fail(str(exc))
    except Exception as exc:
        return connectivity.from_exception("Klaviyo", exc)

    if list_id:
        match = next((l for l in lists if l["id"] == list_id.strip()), None)
        if match is None:
            names = ", ".join(
                f'{l["name"]} ({l["id"]})' for l in lists[:10]
            ) or "none found"
            return connectivity.fail(
                f'No list with ID "{list_id}" on this account. Lists '
                f"Klaviyo does report: {names}."
            )
        result = connectivity.ok(
            f'Connected. Watching list "{match["name"]}" ({match["id"]}).'
        )
    else:
        result = connectivity.ok(
            f"Connected. Klaviyo returned {len(lists)} list(s)."
        )
    result.lists = lists
    return result
