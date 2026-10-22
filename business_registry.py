"""Native local business identity and active-business selection.

Businesses are bookkeeping scopes, not integration objects. Existing Wave-
era IDs are preserved during the one-time local migration so historical
``business_id`` stamps keep resolving; new businesses receive local UUIDs.
"""

import json
import uuid

import db


BUSINESSES_KEY = "businesses"
ACTIVE_BUSINESS_KEY = "active_business_id"

LEGACY_BUSINESSES_KEY = "wave_businesses"
LEGACY_ACTIVE_BUSINESS_KEY = "wave_active_business_id"
LEGACY_CATEGORIES_KEY = "wave_categories_cache"
LEGACY_TOKEN_SECRET_NAME = "wave.api_token"


def _valid_businesses(raw):
    if not raw:
        return []
    try:
        values = json.loads(raw)
    except (TypeError, ValueError):
        return []
    result = []
    for value in values:
        if not isinstance(value, dict):
            continue
        business_id = str(value.get("id") or "").strip()
        name = str(value.get("name") or "").strip()
        if business_id and name:
            result.append({"id": business_id, "name": name})
    return result


def _save(businesses):
    db.set_setting(BUSINESSES_KEY, json.dumps(businesses))


def migrate_legacy_state():
    """Preserve local identity, then remove obsolete Wave-only state."""
    db.init_db()
    businesses = _valid_businesses(db.get_setting(BUSINESSES_KEY))
    if not businesses:
        businesses = _valid_businesses(db.get_setting(LEGACY_BUSINESSES_KEY))
        if not businesses:
            name = (db.get_setting("business_name") or "").strip()
            if name:
                # Some older installs retained only the active ID and display
                # name. Reuse that ID so existing transaction stamps still
                # resolve instead of replacing it with a fresh local UUID.
                legacy_active = db.get_setting(LEGACY_ACTIVE_BUSINESS_KEY)
                businesses = [{"id": legacy_active or str(uuid.uuid4()),
                               "name": name}]
        if businesses:
            _save(businesses)

    active_id = db.get_setting(ACTIVE_BUSINESS_KEY)
    ids = {business["id"] for business in businesses}
    if active_id not in ids:
        legacy_active = db.get_setting(LEGACY_ACTIVE_BUSINESS_KEY)
        active_id = legacy_active if legacy_active in ids else None
        if active_id is None and businesses:
            active_id = businesses[0]["id"]
        if active_id:
            db.set_setting(ACTIVE_BUSINESS_KEY, active_id)

    db.delete_setting(LEGACY_BUSINESSES_KEY)
    db.delete_setting(LEGACY_ACTIVE_BUSINESS_KEY)
    db.delete_setting(LEGACY_CATEGORIES_KEY)
    db.delete_credential(LEGACY_TOKEN_SECRET_NAME)
    return businesses


def list_businesses():
    migrate_legacy_state()
    return _valid_businesses(db.get_setting(BUSINESSES_KEY))


def active_business():
    businesses = list_businesses()
    if not businesses:
        return None
    active_id = db.get_setting(ACTIVE_BUSINESS_KEY)
    for business in businesses:
        if business["id"] == active_id:
            return business
    db.set_setting(ACTIVE_BUSINESS_KEY, businesses[0]["id"])
    return businesses[0]


def set_active_business(business_id):
    for business in list_businesses():
        if business["id"] == business_id:
            db.set_setting(ACTIVE_BUSINESS_KEY, business_id)
            db.set_setting("business_name", business["name"])
            return True
    return False


def business_name_by_id(business_id):
    for business in list_businesses():
        if business["id"] == business_id:
            return business["name"]
    return None


def upsert_business(name, business_id=None, make_active=False):
    """Create or rename one local business and return its stable record."""
    clean_name = (name or "").strip()
    if not clean_name:
        raise ValueError("Enter a business name.")
    businesses = list_businesses()
    business_id = (business_id or "").strip() or str(uuid.uuid4())
    for business in businesses:
        if business["id"] == business_id:
            business["name"] = clean_name
            record = business
            break
    else:
        record = {"id": business_id, "name": clean_name}
        businesses.append(record)
    _save(businesses)
    if make_active or len(businesses) == 1:
        set_active_business(business_id)
    return record


def remove_business(business_id):
    active = active_business()
    if active and active["id"] == business_id:
        raise RuntimeError("Switch to another business before removing the active one.")
    businesses = [business for business in list_businesses()
                  if business["id"] != business_id]
    _save(businesses)
