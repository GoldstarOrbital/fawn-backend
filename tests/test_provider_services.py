"""Unit tests for the provider service guards.

Every provider client must raise a clear <Provider>NotConfigured error when
its credentials are unset, instead of firing an unauthenticated HTTP request.
This is what makes the whole stack safe to deploy before real contracts land.
"""
import asyncio

import pytest

from services import alpaca as alpaca_svc
from services import plaid as plaid_svc


def test_alpaca_guard(monkeypatch):
    monkeypatch.setattr("services.alpaca.settings.alpaca_api_key", "")
    monkeypatch.setattr("services.alpaca.settings.alpaca_api_secret", "")
    with pytest.raises(alpaca_svc.AlpacaNotConfigured):
        asyncio.run(alpaca_svc.get_account("alp_x"))


def test_plaid_guard(monkeypatch):
    monkeypatch.setattr("services.plaid.settings.plaid_client_id", "")
    monkeypatch.setattr("services.plaid.settings.plaid_secret", "")
    with pytest.raises(plaid_svc.PlaidNotConfigured):
        asyncio.run(plaid_svc.create_link_token("user_x"))


def test_alpaca_place_order_rejects_ambiguous_amount(monkeypatch):
    # Configure so the guard passes and the notional/qty XOR check is what trips.
    monkeypatch.setattr("services.alpaca.settings.alpaca_api_key", "k")
    monkeypatch.setattr("services.alpaca.settings.alpaca_api_secret", "s")
    with pytest.raises(ValueError):
        asyncio.run(alpaca_svc.place_order("alp_x", "AAPL", "buy", notional=10, qty=1))
