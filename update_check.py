"""
Update notifications for Klearing.

On launch (and at most once a day after that), a background thread
asks GitHub's public releases API whether a newer version exists and
caches the answer in settings; the dashboard shows a small banner
linking to the release page when one does.

Deliberately NOT an auto-updater: silently replacing an unsigned
binary on someone's machine is the exact behavior malware has, and
users should treat it with suspicion. Updating stays a deliberate
download-and-run.

Every failure path is silent-but-logged — a checker that can break
the app it checks would be worse than no checker.
"""

import json
import threading
from datetime import datetime, timedelta, timezone

import requests

import db
from config import APP_VERSION, GITHUB_REPO

LATEST_RELEASE_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

CACHE_KEY = "update_check_cache"   # JSON {checked_at, latest, url}
CHECK_EVERY = timedelta(hours=24)


def _parse_version(text):
    """'v0.1.0' / '0.1.0' → (0, 1, 0); None when it isn't a version."""
    text = (text or "").strip().lstrip("vV")
    parts = text.split(".")
    try:
        return tuple(int(p) for p in parts[:3])
    except ValueError:
        return None


def _cache():
    db.init_db()
    raw = db.get_setting(CACHE_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def check_now():
    """One live check against GitHub; stores the result. Safe to call
    from anywhere — never raises."""
    try:
        response = requests.get(
            LATEST_RELEASE_URL, timeout=10,
            headers={"Accept": "application/vnd.github+json"},
        )
        if response.status_code != 200:
            return  # no releases yet, rate-limited, offline — all fine
        payload = response.json()
        db.set_setting(CACHE_KEY, json.dumps({
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "latest": payload.get("tag_name", ""),
            "url": payload.get("html_url", ""),
        }))
    except Exception as exc:
        print(f"Update check skipped: {exc}")


def check_soon_if_due():
    """Kick off a background check when the cache is stale. Returns
    immediately; the page render never waits on GitHub."""
    cached = _cache()
    if cached:
        try:
            checked = datetime.fromisoformat(cached["checked_at"])
            if datetime.now(timezone.utc) - checked < CHECK_EVERY:
                return
        except (KeyError, ValueError):
            pass
    thread = threading.Thread(target=check_now, daemon=True)
    thread.start()


def available_update():
    """{'version', 'url'} when the cached latest release is newer than
    this build, else None."""
    cached = _cache()
    if not cached:
        return None
    latest = _parse_version(cached.get("latest"))
    current = _parse_version(APP_VERSION)
    if latest and current and latest > current:
        return {"version": cached["latest"], "url": cached.get("url", "")}
    return None
