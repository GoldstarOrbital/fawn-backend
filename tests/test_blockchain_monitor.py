"""Regression tests for services/blockchain_monitor.py's on-chain balance math.

Covers three real bugs found via live production deposit tests on 2026-07-16:
1. Raw-units-to-cents conversion was off by 100x (divided by 10**6 instead
   of 10**4), silently undervaluing every on-chain USDC balance.
2. The monitor only watched the bridged USDC.e contract on Polygon, missing
   deposits sent as native USDC (what Robinhood, Coinbase, and most modern
   senders actually send).
3. The monitor only watched Polygon at all -- a real deposit on Base
   ($8.01 USDC) was completely invisible, on top of Polygon's own $2.00,
   even though both sit at the same wallet address.
"""
import pytest

from services import blockchain_monitor as bm


class _FakeRPCClient:
    """Returns a fixed balanceOf() result depending on which contract was called."""

    def __init__(self, chain, balances_by_contract):
        self.chain = chain
        self.balances_by_contract = balances_by_contract

    async def call(self, method, params):
        contract = params[0]["to"]
        raw = self.balances_by_contract.get(contract, 0)
        return hex(raw)


def _patch_chain(monkeypatch, chain, balances_by_contract):
    monkeypatch.setitem(
        bm._rpc_clients, chain, _FakeRPCClient(chain, balances_by_contract)
    )


@pytest.mark.asyncio
async def test_raw_units_to_cents_conversion_is_correct(monkeypatch):
    # 1.000000 USDC (6 decimals) = 1_000_000 raw units = $1.00 = 100 cents.
    contracts = bm.CHAINS["polygon"]["contracts"]
    _patch_chain(monkeypatch, "polygon", {contracts["usdc_native"]: 1_000_000, contracts["usdc_bridged"]: 0})
    _patch_chain(monkeypatch, "base", {})

    balance_cents = await bm._get_usdc_balance("0x" + "1" * 40)
    assert balance_cents == 100


@pytest.mark.asyncio
async def test_native_and_bridged_variants_are_summed_within_a_chain(monkeypatch):
    # $1.00 native + $2.50 bridged on Polygon should combine to $3.50.
    contracts = bm.CHAINS["polygon"]["contracts"]
    _patch_chain(monkeypatch, "polygon", {contracts["usdc_native"]: 1_000_000, contracts["usdc_bridged"]: 2_500_000})
    _patch_chain(monkeypatch, "base", {})

    balance_cents = await bm._get_usdc_balance("0x" + "2" * 40)
    assert balance_cents == 350


@pytest.mark.asyncio
async def test_native_only_deposit_is_still_detected(monkeypatch):
    # Reproduces the exact real-world case: funds only in the native
    # contract, none in bridged. Must not be treated as a zero balance.
    contracts = bm.CHAINS["polygon"]["contracts"]
    _patch_chain(monkeypatch, "polygon", {contracts["usdc_native"]: 5_000_000, contracts["usdc_bridged"]: 0})
    _patch_chain(monkeypatch, "base", {})

    balance_cents = await bm._get_usdc_balance("0x" + "3" * 40)
    assert balance_cents == 500


@pytest.mark.asyncio
async def test_balances_are_summed_across_chains(monkeypatch):
    # Reproduces the exact real-world case: $2.00 on Polygon + $8.01 on
    # Base at the SAME wallet address must combine to one total, not just
    # show whichever chain happens to be checked first (or only Polygon).
    polygon_contracts = bm.CHAINS["polygon"]["contracts"]
    base_contracts = bm.CHAINS["base"]["contracts"]

    _patch_chain(monkeypatch, "polygon", {
        polygon_contracts["usdc_native"]: 2_000_000,   # $2.00
        polygon_contracts["usdc_bridged"]: 0,
    })
    _patch_chain(monkeypatch, "base", {
        base_contracts["usdc_native"]: 8_009_315,       # $8.009315
        base_contracts["usdc_bridged"]: 0,
    })

    balance_cents = await bm._get_usdc_balance("0x" + "4" * 40)
    assert balance_cents == 200 + 800  # $2.00 + $8.00 (raw truncates to whole cents)


@pytest.mark.asyncio
async def test_one_chain_failing_does_not_zero_out_the_other(monkeypatch):
    # A transient RPC failure on one chain must not wipe out a real balance
    # detected on another chain.
    class _FailingRPCClient:
        chain = "base"
        async def call(self, method, params):
            return None  # simulates every endpoint on this chain failing

    polygon_contracts = bm.CHAINS["polygon"]["contracts"]
    _patch_chain(monkeypatch, "polygon", {polygon_contracts["usdc_native"]: 1_000_000, polygon_contracts["usdc_bridged"]: 0})
    monkeypatch.setitem(bm._rpc_clients, "base", _FailingRPCClient())

    balance_cents = await bm._get_usdc_balance("0x" + "5" * 40)
    assert balance_cents == 100  # Polygon's $1.00 still comes through
