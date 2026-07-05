"""Tier 2: sends to non-FAWN banks/cards.

Deliberately stubbed. Which rail we use — FedNow/RTP (real-time
bank-to-bank), push-to-card (Visa Direct/Mastercard Send), Stripe Treasury
Outbound Payments (ACH/wire), or same-day ACH fallback — depends on which
of Stripe's outbound rails get approved for the platform's Treasury
program, which is still being confirmed. Do not hardcode a rail;
implement a real provider behind this interface once that answer lands.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum


class ExternalRail(str, Enum):
    FEDNOW_RTP = "fednow_rtp"
    PUSH_TO_CARD = "push_to_card"
    SAME_DAY_ACH = "same_day_ach"


class ExternalSendProvider(ABC):
    """Implement one of these per confirmed rail once Stripe's Treasury
    outbound-payment capabilities are confirmed for the platform. Router
    code should depend only on this interface, never on a specific rail,
    so swapping/adding providers later doesn't touch the P2P router."""

    rail: ExternalRail

    @abstractmethod
    async def send(
        self,
        sender_account_id: str,
        destination_account_number: str,
        destination_routing_number: str,
        amount_cents: int,
        idempotency_key: str,
    ) -> dict:
        ...


class NotYetSupportedExternalSendProvider(ExternalSendProvider):
    """Default/only provider until a rail is confirmed. Always raises —
    callers should surface this as a clear 501, not a generic failure."""

    rail = None

    async def send(self, *args, **kwargs) -> dict:
        raise NotImplementedError(
            "External (non-FAWN) sends aren't available yet — we're confirming which rail "
            "(FedNow/RTP, push-to-card, or same-day ACH) is available through Stripe Treasury."
        )


def get_external_send_provider() -> ExternalSendProvider:
    """Swap this once a rail is confirmed, e.g. return FedNowRtpProvider()."""
    return NotYetSupportedExternalSendProvider()
