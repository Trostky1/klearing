"""
Overdue lead follow-up detection for Klearing.

Runs as part of the daily check (called from check_bank.run_daily,
also runnable standalone). Entirely read-only against Klaviyo and
Gmail — it never emails a lead itself; it only drafts follow-ups that
wait for human review on the Follow-ups tab.

Flow:
1. Pull profiles from the configured Klaviyo list (silently skips
   with a notice when Klaviyo isn't connected — it's optional).
2. Keep only leads whose inquiry is 48+ hours old and who aren't
   already tracked in quote_followups.
3. For each, search Gmail (IMAP: Inbox + Sent) for any message FROM
   the lead since their submission — a reply means no follow-up is
   needed.
4. Leads with no reply get a drafted follow-up inserted as
   status="pending_review" for the Follow-ups tab.

Lead properties: name/email come from the Klaviyo profile itself.
If the profile carries "program", "rosterSize", or "sportGoal"
properties (set by whatever form feeds the list), the draft uses
them; when absent the draft stays generic — no form fields are
required beyond an email address.
"""

import imaplib
import sys
from datetime import datetime, timedelta, timezone

import requests

import db
import email_utils
import klaviyo_client

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

OVERDUE_AFTER_HOURS = 48
IMAP_HOST = "imap.gmail.com"
IMAP_FOLDERS = ("INBOX", "[Gmail]/Sent Mail")

BUSINESS_NAME_KEY = "business_name"


def business_name():
    db.init_db()
    return (db.get_setting(BUSINESS_NAME_KEY) or "").strip() or "our team"


def parse_lead(profile):
    """Flatten a Klaviyo profile into the fields we track. Returns None
    if it's missing an email (nothing to follow up on)."""
    attributes = profile.get("attributes", {}) or {}
    properties = attributes.get("properties", {}) or {}

    email = attributes.get("email") or properties.get("email")
    if not email:
        return None

    # Forms commonly stamp a timestamp property; fall back to the
    # profile's created date if it's absent.
    submitted_raw = (properties.get("timestamp")
                     or attributes.get("created") or "")
    submitted_at = str(submitted_raw)

    name = (properties.get("name")
            or f"{attributes.get('first_name') or ''} {attributes.get('last_name') or ''}".strip()
            or email)

    return {
        "klaviyo_profile_id": profile.get("id"),
        "name": name,
        "email": email,
        "program": str(properties.get("program") or ""),
        "roster_size": str(properties.get("rosterSize") or ""),
        "sport_goal": str(properties.get("sportGoal") or ""),
        "submitted_at": submitted_at,
    }


def parse_submitted_at(value):
    """Best-effort ISO timestamp parse; returns an aware UTC datetime
    or None."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def open_imap():
    credentials = email_utils.email_credentials()
    if credentials is None:
        raise RuntimeError(
            "Email isn't set up (Gmail address / app password missing), "
            "so replies can't be checked."
        )
    address, password = credentials
    imap = imaplib.IMAP4_SSL(IMAP_HOST, 993)
    imap.login(address, password)
    return imap


def lead_has_replied(imap, lead_email, since):
    """
    True if any message FROM the lead exists since their submission.
    Searches Inbox and Sent (Sent will only match if the lead somehow
    appears as a sender there, e.g. forwarded threads — harmless).
    """
    since_str = since.strftime("%d-%b-%Y")
    for folder in IMAP_FOLDERS:
        status, _ = imap.select(f'"{folder}"', readonly=True)
        if status != "OK":
            continue
        status, data = imap.search(None, "FROM", f'"{lead_email}"',
                                   "SINCE", since_str)
        if status == "OK" and data and data[0].split():
            return True
    return False


def draft_followup(lead):
    """A friendly follow-up draft. Generic when the profile has no
    extra properties; specific when it does. No pricing commitments."""
    business = business_name()
    sender = email_utils.sender_name()

    if lead["program"]:
        subject = f"Following up on {lead['program']} and {business}"
        inquiry_ref = f"the inquiry you sent over for {lead['program']}"
    else:
        subject = f"Following up on your {business} inquiry"
        inquiry_ref = "the inquiry you sent over"

    roster_line = ""
    if lead["roster_size"]:
        roster_line = f" for a group of about {lead['roster_size']} people"

    goal_line = ""
    if lead["sport_goal"]:
        goal_line = (f" You mentioned your focus is {lead['sport_goal']} — "
                     f"happy to speak to exactly that.")

    first_name = lead["name"].split()[0] if lead["name"].strip() else "there"

    body = (
        f"Hi {first_name},\n"
        f"\n"
        f"Just wanted to follow up on {inquiry_ref}{roster_line}.{goal_line}\n"
        f"\n"
        f"Happy to answer any questions, or to put together options that "
        f"fit your needs and budget.\n"
        f"\n"
        f"Is there a good time this week for a quick call?\n"
        f"\n"
        f"Best,\n"
        f"{sender}\n"
        f"{business}"
    )
    return subject, body


def run_followup_check():
    """
    Returns a summary dict: counts of new drafts, already-replied
    skips, and whether Klaviyo was configured at all.
    """
    db.init_db()

    config = klaviyo_client.load_config()
    if config is None:
        print("Lead follow-ups: Klaviyo isn't connected — skipping.")
        return {"configured": False, "drafted": 0, "replied": 0}

    api_key, list_id = config
    profiles = klaviyo_client.fetch_list_profiles(api_key, list_id)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=OVERDUE_AFTER_HOURS)

    candidates = []
    for profile in profiles:
        lead = parse_lead(profile)
        if lead is None:
            continue
        submitted = parse_submitted_at(lead["submitted_at"])
        if submitted is None or submitted > cutoff:
            continue  # too recent (or unparseable) — not overdue yet
        if db.quote_followup_exists(lead["klaviyo_profile_id"], lead["email"]):
            continue  # already tracked
        lead["submitted_dt"] = submitted
        candidates.append(lead)

    if not candidates:
        print("Lead follow-ups: no new overdue leads.")
        return {"configured": True, "drafted": 0, "replied": 0}

    drafted = replied = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    imap = open_imap()
    try:
        for lead in candidates:
            if lead_has_replied(imap, lead["email"], lead["submitted_dt"]):
                # They already wrote back — nothing to follow up on, and
                # per spec we simply don't add them.
                replied += 1
                print(f"  - {lead['email']}: already replied, skipping.")
                continue
            subject, body = draft_followup(lead)
            db.insert_quote_followup(
                klaviyo_profile_id=lead["klaviyo_profile_id"],
                name=lead["name"],
                email=lead["email"],
                program=lead["program"] or "General inquiry",
                roster_size=lead["roster_size"],
                sport_goal=lead["sport_goal"],
                submitted_at=lead["submitted_at"],
                draft_subject=subject,
                draft_body=body,
                gmail_thread_checked_at=now_iso,
            )
            drafted += 1
            print(f"  - {lead['email']}: no reply found, follow-up drafted.")
    finally:
        try:
            imap.logout()
        except Exception:
            pass

    print(f"Lead follow-ups: {drafted} drafted for review, "
          f"{replied} already replied.")
    return {"configured": True, "drafted": drafted, "replied": replied}


def main() -> int:
    try:
        run_followup_check()
    except (requests.exceptions.RequestException, RuntimeError,
            imaplib.IMAP4.error) as exc:
        print(f"Lead follow-up check failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
