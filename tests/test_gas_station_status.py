"""Tests for GET /status/gas-station — the ops endpoint that tells the
founder which wallet to fund for gas sponsorship.

Must expose ONLY the derived public address and (mocked) native balances,
never the private key. No real network calls.
"""
import secrets

from eth_account import Account

from config import settings
from services import onchain_send


GAS_KEY = "0x" + secrets.token_hex(32)
GAS_ADDRESS = Account.from_key(GAS_KEY).address


def test_unconfigured_reports_disabled(client, monkeypatch):
    monkeypatch.setattr(settings, "gas_station_private_key", "")
    r = client.get("/status/gas-station")
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is False
    assert body["address"] is None


def test_reports_address_and_balances_never_the_key(client, monkeypatch):
    monkeypatch.setattr(settings, "gas_station_private_key", GAS_KEY)

    async def fake_balance(chain, address):
        assert address == GAS_ADDRESS
        return 2 * 10**18  # 2 native tokens

    monkeypatch.setattr(onchain_send, "_get_native_balance", fake_balance)

    r = client.get("/status/gas-station")
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is True
    assert body["address"] == GAS_ADDRESS
    assert all(b["native"] == 2.0 for b in body["balances"].values())
    # The private key must never appear anywhere in the response.
    assert GAS_KEY not in r.text
    assert GAS_KEY[2:] not in r.text


def test_rpc_failure_is_reported_not_fatal(client, monkeypatch):
    monkeypatch.setattr(settings, "gas_station_private_key", GAS_KEY)

    async def broken_balance(chain, address):
        raise RuntimeError("rpc down")

    monkeypatch.setattr(onchain_send, "_get_native_balance", broken_balance)

    r = client.get("/status/gas-station")
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is True
    assert body["address"] == GAS_ADDRESS
    assert all(b["native"] is None for b in body["balances"].values())
