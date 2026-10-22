"""Offline regression coverage for Feature A's local notifications."""

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import briefing_notifications as notifications  # noqa: E402
import briefing_signals  # noqa: E402


def check(name, condition):
    print(("  PASS  " if condition else "  FAIL  ") + name)
    return 0 if condition else 1


def signal(severity, sentence):
    return briefing_signals.make_briefing_signal(
        severity, sentence, "https://example.myshopify.com/admin/orders")


def run():
    failures = 0
    urgent = [
        signal("urgent", "Order 81 contains $900.00 of private detail."),
        signal("urgent", "Order 82 contains $400.00 of private detail."),
        signal("notable", "Sales rose by 31% overnight."),
        signal("informational", "Shopify recorded 14 orders today."),
    ]
    message = notifications.notification_message(urgent)
    failures += check(
        "toast uses only the strongest tier and its count",
        message == "2 urgent items need your attention in Klearing.")
    failures += check(
        "toast contains no signal-specific content",
        all(token not in message for token in (
            "81", "82", "$900.00", "$400.00", "31%", "14", "Shopify")))
    failures += check(
        "singular notable copy is grammatical",
        notifications.notification_message([
            signal("notable", "Sales are moving faster than usual.")
        ]) == "1 notable item is waiting in Klearing.")
    failures += check(
        "informational-only copy remains tier-and-count-only",
        notifications.notification_message([
            signal("informational", "Shopify recorded 3 orders today.")
        ]) == "1 informational update is available in Klearing.")
    failures += check(
        "no signals produce no notification copy",
        notifications.notification_message([]) is None)

    original_windows = notifications._send_windows
    calls = []
    notifications._send_windows = lambda text: calls.append(("Windows", text))
    try:
        windows_sent = notifications.send_briefing_notification(
            urgent, system="Windows")
        mac_skipped = notifications.send_briefing_notification(
            urgent, system="Darwin")
        linux_sent = notifications.send_briefing_notification(
            urgent, system="Linux")
        empty_sent = notifications.send_briefing_notification(
            [], system="Windows")
    finally:
        notifications._send_windows = original_windows

    failures += check(
        "Windows receives the content-free message",
        windows_sent and calls == [("Windows", message)])
    failures += check(
        "unsupported systems and empty signal sets skip cleanly",
        not mac_skipped and not linux_sent and not empty_sent)

    daily_source = (ROOT / "check_bank.py").read_text(encoding="utf-8")
    sync_at = daily_source.index(
        'error_alerts.run_step("Shopify sync", shopify_client.sync, failures)')
    toast_at = daily_source.index(
        "briefing_notifications.send_briefing_notification(")
    failures += check(
        "daily toast runs only after the existing Shopify refresh",
        sync_at < toast_at)
    toast_block = daily_source[toast_at:toast_at + 220]
    failures += check(
        "daily toast uses the Shopify-only collector",
        "get_toast_briefing_signals()" in toast_block
        and "plaid" not in toast_block.lower()
        and "get_home_briefing_signals" not in toast_block)

    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    spec = (ROOT / "build" / "klearing.spec").read_text(encoding="utf-8")
    failures += check(
        "the notification dependency is Windows-scoped",
        'windows-toasts==1.3.1; sys_platform == "win32"' in requirements
        and "UserNotifications" not in requirements)
    failures += check(
        "PyInstaller collects only the Windows notification bridge",
        'collect_submodules("windows_toasts")' in spec
        and '"winrt.windows.ui.notifications"' in spec
        and '"UserNotifications"' not in spec)

    if failures:
        print(f"FAIL: {failures} briefing notification checks failed.")
        return 1
    print("PASS: Feature A briefing notification checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
