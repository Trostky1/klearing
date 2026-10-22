"""Shared contract and provider selection for Home briefing signals.

Providers read only data already cached on this device.  The two collection
functions intentionally differ: Home includes Shopify and Plaid, while the
future scheduled toast path is Shopify-only because the daily job does not
refresh Plaid.
"""

from typing import Literal, TypedDict, cast
from urllib.parse import urlparse


BriefingSeverity = Literal["urgent", "notable", "informational"]


class BriefingSignal(TypedDict):
    severity: BriefingSeverity
    plain_language_sentence: str
    deep_link: str


SEVERITY_ORDER: dict[BriefingSeverity, int] = {
    "urgent": 0,
    "notable": 1,
    "informational": 2,
}


def make_briefing_signal(
        severity: BriefingSeverity,
        plain_language_sentence: str,
        deep_link: str) -> BriefingSignal:
    """Build one signal while enforcing the small cross-provider contract."""
    if severity not in SEVERITY_ORDER:
        raise ValueError(f"Unsupported briefing severity: {severity}")

    sentence = " ".join((plain_language_sentence or "").split())
    if not sentence or sentence[-1] not in ".!?" or "\n" in sentence:
        raise ValueError("A briefing signal must contain one complete sentence.")

    parsed = urlparse(deep_link)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("A briefing signal deep-link must be an HTTPS URL.")

    return {
        "severity": cast(BriefingSeverity, severity),
        "plain_language_sentence": sentence,
        "deep_link": deep_link,
    }


def get_home_briefing_signals() -> list[BriefingSignal]:
    """Collect cached signals from every provider in Feature A's scope."""
    # Lazy imports avoid a module cycle: each provider imports the contract.
    import plaid_client
    import shopify_client

    return [
        *shopify_client.get_briefing_signals(),
        *plaid_client.get_briefing_signals(),
    ]


def get_toast_briefing_signals() -> list[BriefingSignal]:
    """Collect only signals refreshed by the scheduled daily check."""
    import shopify_client

    return shopify_client.get_briefing_signals()
