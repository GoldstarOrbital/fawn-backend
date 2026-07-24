"""SnapTrade brokerage-connection client; separate from FAWN custody."""
from __future__ import annotations

import asyncio
from typing import Any

from cryptography.fernet import Fernet
from snaptrade_client import Configuration, SnapTrade, SnapTradeAuth
from config import settings


class SnapTradeNotConfigured(RuntimeError):
    pass


class SnapTradeError(RuntimeError):
    pass


def _require_configured() -> None:
    if not settings.snaptrade_client_id or not settings.snaptrade_consumer_key:
        raise SnapTradeNotConfigured("SnapTrade credentials are not configured.")


def _client() -> SnapTrade:
    _require_configured()
    return SnapTrade(Configuration(auth=SnapTradeAuth.commercial_api_key(
        consumer_key=settings.snaptrade_consumer_key,
        client_id=settings.snaptrade_client_id,
    )))


def encrypt_user_secret(secret: str) -> bytes:
    if not settings.fawn_encryption_key:
        raise SnapTradeError("FAWN_ENCRYPTION_KEY is required to store brokerage credentials.")
    return Fernet(settings.fawn_encryption_key).encrypt(secret.encode())


def decrypt_user_secret(ciphertext: bytes) -> str:
    if not settings.fawn_encryption_key:
        raise SnapTradeError("FAWN_ENCRYPTION_KEY is required to use brokerage connections.")
    return Fernet(settings.fawn_encryption_key).decrypt(ciphertext).decode()


def _payload(response: Any) -> Any:
    return getattr(response, "data", response)


async def register_user(snaptrade_user_id: str) -> dict:
    return dict(await asyncio.to_thread(lambda: _payload(_client().authentication.register_snap_trade_user(user_id=snaptrade_user_id))))


async def create_portal(user_id: str, user_secret: str, redirect_uri: str | None = None) -> dict:
    def call():
        return _payload(_client().authentication.login_snap_trade_user(
            user_id=user_id, user_secret=user_secret,
            connection_type=settings.snaptrade_connection_type,
            immediate_redirect=bool(redirect_uri), custom_redirect=redirect_uri,
            dark_mode=True, connection_portal_version="v4",
        ))
    return dict(await asyncio.to_thread(call))


async def list_connections(user_id: str, user_secret: str) -> list:
    def call():
        value = _payload(_client().connections.list_brokerage_authorizations(user_id=user_id, user_secret=user_secret))
        return value if isinstance(value, list) else value.get("data", [])
    return await asyncio.to_thread(call)


async def list_accounts(user_id: str, user_secret: str) -> list:
    def call():
        value = _payload(_client().account_information.list_user_accounts(user_id=user_id, user_secret=user_secret))
        return value if isinstance(value, list) else value.get("data", [])
    return await asyncio.to_thread(call)
