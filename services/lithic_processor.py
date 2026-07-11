"""
Lithic processor integration for card issuance and auth webhooks.
- Real-time authorization via webhook callbacks
- Idempotent card issuance
- Webhook signature verification
"""

import hashlib
import hmac
import json
import logging
from typing import Optional, Dict, Any
import aiohttp

from core.config import settings

logger = logging.getLogger(__name__)


class LithicException(Exception):
    """Lithic processor error"""
    pass


class LithicProcessor:
    """
    Lithic card processor integration.
    - Issue virtual cards (instant)
    - Receive auth webhooks
    - Signature verification
    """

    def __init__(self):
        self.base_url = settings.lithic_base_url or "https://api.sandbox.lithic.com"
        self.api_key = settings.lithic_api_key
        self.webhook_secret = settings.lithic_webhook_secret

        if not self.api_key and not settings.allow_unsigned_lithic_webhooks:
            logger.warning("Lithic API key not configured")

    async def issue_card(
        self,
        user_id: str,
        wallet_address: str,
    ) -> Dict[str, str]:
        """
        Issue virtual card via Lithic API.

        Returns:
            {
                'lithic_card_token': str (opaque card ID),
                'card_last_four': str,
                'card_brand': str,
                'card_status': str,
            }

        Raises:
            LithicException: API error
        """
        if not self.api_key:
            raise LithicException("Lithic API key not configured")

        # Idempotency: use user_id + wallet as key
        idempotency_key = f"card_issue_{user_id}_{wallet_address}"

        payload = {
            "type": "VIRTUAL",
            "account_token": user_id,
            "memo": f"USDC Wallet {wallet_address[:8]}...",
        }

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/v1/cards",
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 201:
                        error_text = await resp.text()
                        logger.error(f"Lithic card issue failed: {resp.status} {error_text}")
                        raise LithicException(f"Lithic API error: {resp.status}")

                    data = await resp.json()
                    return {
                        'lithic_card_token': data.get('token'),
                        'card_last_four': data.get('last_four', '0000'),
                        'card_brand': 'VISA',  # Lithic default
                        'card_status': data.get('state', 'OPEN'),
                    }
        except aiohttp.ClientError as e:
            logger.error(f"Lithic API connection error: {e}")
            raise LithicException(f"Connection error: {e}")

    def verify_webhook_signature(self, body: str, signature: str) -> bool:
        """
        Verify Lithic webhook signature (HMAC-SHA256).

        Args:
            body: Raw webhook payload (string)
            signature: X-Lithic-Signature header value

        Returns:
            True if valid, False otherwise
        """
        if not self.webhook_secret and not settings.allow_unsigned_lithic_webhooks:
            logger.warning("Lithic webhook secret not configured, allowing unsigned webhooks")
            return True

        if settings.allow_unsigned_lithic_webhooks:
            logger.debug("Unsigned Lithic webhooks allowed (DEV MODE)")
            return True

        # Compute HMAC-SHA256
        expected_signature = hmac.new(
            self.webhook_secret.encode(),
            body.encode(),
            hashlib.sha256
        ).hexdigest()

        # Compare (timing-safe)
        return hmac.compare_digest(signature, expected_signature)

    def parse_webhook_payload(self, body: str) -> Dict[str, Any]:
        """Parse and validate webhook payload"""
        try:
            return json.loads(body)
        except json.JSONDecodeError as e:
            logger.error(f"Invalid webhook JSON: {e}")
            raise LithicException(f"Invalid JSON payload: {e}")

    def extract_auth_request(self, webhook_payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Extract authorization request from webhook.

        Lithic webhook format (simplified):
        {
            'type': 'card_transaction.updated',
            'data': {
                'token': 'card_token',
                'events': [
                    {
                        'type': 'AUTHORIZATION',
                        'amount': 100,
                        'merchant': {...},
                        'network_identifiers': {
                            'processor_transaction_id': 'xyz123'
                        }
                    }
                ]
            }
        }
        """
        if webhook_payload.get('type') != 'card_transaction.updated':
            return None

        data = webhook_payload.get('data', {})
        events = data.get('events', [])

        for event in events:
            if event.get('type') == 'AUTHORIZATION':
                merchant = event.get('merchant', {})
                network_ids = event.get('network_identifiers', {})

                return {
                    'card_token': data.get('token'),
                    'processor_transaction_id': network_ids.get('processor_transaction_id'),
                    'idempotency_key': network_ids.get('trace_number'),  # Merchant trace
                    'merchant_name': merchant.get('merchant_name', 'Unknown'),
                    'merchant_mcc': merchant.get('mcc_code'),
                    'amount_cents': event.get('amount', 0),
                }

        return None
