"""
Shared email sending for Klearing.

One place for the Gmail SMTP logic, used by the daily check, the cash
alert, and the weekly digest. The Gmail address and notification
recipient live in settings; the app password lives in the secrets
store (it's a credential).
"""

import smtplib
import socket
from email.message import EmailMessage

import connectivity
import db
import secrets_store

GMAIL_ADDRESS_KEY = "gmail_address"
NOTIFY_EMAIL_KEY = "notify_email"
SENDER_NAME_KEY = "sender_name"
APP_PASSWORD_SECRET = "gmail.app_password"

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465


def email_credentials():
    """(gmail_address, app_password), or None when email isn't set up.
    Also used by the IMAP reply check in check_overdue_quotes.py."""
    db.init_db()
    address = (db.get_setting(GMAIL_ADDRESS_KEY) or "").strip()
    password = secrets_store.get_secret(APP_PASSWORD_SECRET)
    if not address or not password:
        return None
    return address, password


def is_configured():
    return email_credentials() is not None


def sender_name():
    """The name used to sign drafted emails; falls back to the
    business name, then the Gmail address."""
    db.init_db()
    name = (db.get_setting(SENDER_NAME_KEY) or "").strip()
    if name:
        return name
    business = (db.get_setting("business_name") or "").strip()
    if business:
        return business
    return (db.get_setting(GMAIL_ADDRESS_KEY) or "").strip()


def send_email(subject, body, to=None, *, attachments=None):
    """
    Send one email via Gmail SMTP.

    By default the recipient is the notification address from Settings
    (falling back to the Gmail address itself) — the self-notification
    path. Pass `to` to send to someone else, e.g. a quote-follow-up
    lead or an invoice recipient. Returns True if an email was sent,
    False if email isn't configured.

    `attachments` is an optional list of (filename, content_bytes,
    subtype) tuples, keyword-only so the six existing positional callers
    are untouched. Every current attachment is a PDF, so maintype is
    fixed at "application"; widen it here if that ever stops being true.

    On an SMTP failure this still raises, unchanged: the daily check,
    weekly digest and alert callers rely on the exception reaching their
    logs. Callers that must not surface a 500 (an interactive send from a
    page) wrap the call in try/except, the way the Settings test-alert
    route already does.
    """
    credentials = email_credentials()
    if credentials is None:
        print("Email not configured (Gmail address / app password not "
              "set up) — skipping email.")
        return False
    sender, password = credentials
    db.init_db()
    recipient = to or (db.get_setting(NOTIFY_EMAIL_KEY) or "").strip() or sender

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    message.set_content(body)

    for filename, content, subtype in (attachments or []):
        message.add_attachment(content, maintype="application",
                               subtype=subtype, filename=filename)

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.login(sender, password)
        smtp.send_message(message)

    count = len(attachments or [])
    suffix = f" with {count} attachment(s)" if count else ""
    print(f"Email sent to {recipient}: {subject}{suffix}")
    return True


def test_connection(address, app_password):
    """Live SMTP login check for the Setup wizard. Returns
    connectivity.TestResult. Logs in and disconnects — sends nothing."""
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) as smtp:
            smtp.login(address, app_password)
    except smtplib.SMTPAuthenticationError:
        return connectivity.fail(
            "Gmail rejected the sign-in. Make sure this is an app "
            "password (16 letters, generated at "
            "myaccount.google.com/apppasswords), not your normal Gmail "
            "password — and that 2-Step Verification is turned on for "
            "the account, which app passwords require."
        )
    except (socket.gaierror, socket.timeout, ConnectionError, OSError):
        return connectivity.fail(
            "Could not reach Gmail's mail server at all (the connection "
            "never got a response).",
            hint=connectivity.NETWORK_HINT,
        )
    except smtplib.SMTPException as exc:
        return connectivity.fail(f"Gmail SMTP error: {exc}")

    return connectivity.ok(f"Signed in to Gmail as {address}.")
