"""Native Windows notifications for Feature A's scheduled briefing.

The scheduled check supplies Shopify-only signals after its existing sync.
Notification text is deliberately content-free: only the highest severity
present and the number of signals at that severity leave the app database.
No provider sentence, amount, order count, or deep-link is included.
"""

import platform
import subprocess
from collections.abc import Sequence

import paths
from briefing_signals import BriefingSignal, SEVERITY_ORDER
from config import APP_NAME


WINDOWS_AUMID = "Klearing.Desktop"
WINDOWS_PROTOCOL = "klearing"
WINDOWS_HOME_URI = f"{WINDOWS_PROTOCOL}://home"


def notification_message(signals: Sequence[BriefingSignal]) -> str | None:
    """Return one tier-and-count-only message for the strongest signals."""
    if not signals:
        return None

    severity = min(signals, key=lambda item: SEVERITY_ORDER[item["severity"]])[
        "severity"]
    count = sum(item["severity"] == severity for item in signals)
    noun = "item" if count == 1 else "items"

    if severity == "urgent":
        verb = "needs" if count == 1 else "need"
        return f"{count} urgent {noun} {verb} your attention in {APP_NAME}."
    if severity == "notable":
        verb = "is" if count == 1 else "are"
        return f"{count} notable {noun} {verb} waiting in {APP_NAME}."

    update = "update" if count == 1 else "updates"
    verb = "is" if count == 1 else "are"
    return f"{count} informational {update} {verb} available in {APP_NAME}."


def _register_windows_identity() -> None:
    """Register per-user toast identity and click-to-open activation."""
    import winreg

    icon = paths.resource_path("assets/klearing.ico")
    with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            rf"SOFTWARE\Classes\AppUserModelId\{WINDOWS_AUMID}") as key:
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
        if icon.exists():
            winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, str(icon.resolve()))

    protocol_root = rf"SOFTWARE\Classes\{WINDOWS_PROTOCOL}"
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, protocol_root) as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, f"URL:{APP_NAME}")
        winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")

    if icon.exists():
        with winreg.CreateKeyEx(
                winreg.HKEY_CURRENT_USER,
                protocol_root + r"\DefaultIcon") as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, str(icon.resolve()))

    command = subprocess.list2cmdline([*paths.app_command(), "%1"])
    with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            protocol_root + r"\shell\open\command") as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, command)


def _send_windows(message: str) -> None:
    from windows_toasts import InteractableWindowsToaster, Toast

    _register_windows_identity()
    toaster = InteractableWindowsToaster(APP_NAME, notifierAUMID=WINDOWS_AUMID)
    toast = Toast([message], launch_action=WINDOWS_HOME_URI, group="briefing")
    toaster.show_toast(toast)


def send_briefing_notification(
        signals: Sequence[BriefingSignal], system: str | None = None) -> bool:
    """Send one Windows toast; return False when there is no work."""
    message = notification_message(signals)
    if message is None:
        return False

    system = system or platform.system()
    if system == "Windows":
        _send_windows(message)
    else:
        print(f"Briefing notification: {system} is not supported - skipping.")
        return False

    print(f"Briefing notification: sent {message}")
    return True
