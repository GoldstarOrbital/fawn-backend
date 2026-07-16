"""Regression tests for services/blockchain_monitor.py's on-chain balance math.

Covers two real bugs found via a live production deposit test on 2026-07-16:
1. Raw-units-to-cents conversion was off by 100x (divided by 10**6 instead
   of 10**4), silently undervaluing every on-chain USDC balance.
2. The monitor only watched the bridged USDC.e contract, missing deposits
   sent as native USDC (what Robinhood, Coinbase, and most modern senders
   actually send) -- those deposits were on-chain but invisible to FAWN.
"""
import pytest

from services import blockchain_monitor as bm


class _FakeRPCClient:
    """Returns a fixed balanceOf() result depending on which contract was called."""

    def __init__(self, balances_by_contract):
        self.balances_by_contract = balances_by_contract

    async def call(self, method, params):
        contract = params[0]["to"]
        raw = self.balances_by_contract.get(contract, 0)
        return hex(raw)


@pytest.mark.asyncio
async def test_raw_units_to_cents_conversion_is_correct(monkeypatch):
    # 1.000000 USDC (6 decimals) = 1_000_000 raw units = $1.00 = 100 cents.
    monkeypatch.setattr(
        bm, "_rpc_client",
        _FakeRPCClient({bm.USDC_CONTRACT_NATIVE: 1_000_000, bm.USDC_CONTRACT_BRIDGED: 0}),
    )
    balance_cents = await bm._get_usdc_balance("0x" + "1" * 40)
    assert balance_cents == 100


@pytest.mark.asyncio
async def test_native_and_bridged_usdc_are_summed(monkeypatch):
    # $1.00 native + $2.50 bridged should combine to $3.50 total.
    monkeypatch.setattr(
        bm, "_rpc_client",
        _FakeRPCClient({bm.USDC_CONTRACT_NATIVE: 1_000_000, bm.USDC_CONTRACT_BRIDGED: 2_500_000}),
    )
    balance_cents = await bm._get_usdc_balance("0x" + "2" * 40)
    assert balance_cents == 350


@pytest.mark.asyncio
async def test_native_only_deposit_is_still_detected(monkeypatch):
    # Reproduces the exact real-world case: funds only in the native
    # contract, none in bridged. Must not be treated as a zero balance.
    monkeypatch.setattr(
        bm, "_rpc_client",
        _FakeRPCClient({bm.USDC_CONTRACT_NATIVE: 5_000_000, bm.USDC_CONTRACT_BRIDGED: 0}),
    )
    balance_cents = await bm._get_usdc_balance("0x" + "3" * 40)
    assert balance_cents == 500
