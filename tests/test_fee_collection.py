"""Tests for the fee-collection fix: envelope encryption for custodial
keys, the treasury wallet, pending_fee_cents accrual on send, and
collect_fees() actually sweeping fees on-chain instead of being a stub.

Background: send_usdc deducts amount+fee from the sender's ledger balance
but has only ever moved `amount` on-chain -- the fee portion was real USDC
that stayed sitting in the sender's own wallet forever, silently drifting
out of sync with the ledger, since collect_fees() was a hardcoded stub
that returned zeros. This file covers the fix: fee accrual is now tracked
per-wallet (CryptoWallet.pending_fee_cents) and collect_fees() sweeps it
to a real treasury wallet with real on-chain transfers.

RPC calls are mocked the same way tests/test_onchain_send.py does --
_FakeChainClient stands in for bm._rpc_clients[chain]; no real network
calls, no real funds.
"""
import secrets
import uuid

import pytest
from eth_account import Account
from cryptography.fernet import Fernet

from database import SessionLocal
from models import User, CryptoWallet, CryptoTransfer, FeeCollection, UserAuditLog
from services import blockchain_monitor as bm
from services import crypto_wallet
from services import onchain_send
from services.crypto_wallet import (
    _encrypt_private_key,
    _encrypt_private_key_envelope,
    _decrypt_private_key,
    _wrap_dek,
    _unwrap_dek,
)
from tests.test_onchain_send import _FakeChainClient, _patch_chain, GAS_STATION_PRIVATE_KEY


def _make_bare_user(db):
    user = User(
        email=f"feecollect_{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        full_name="Fee Collection Tester",
        is_student=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_custodial_user_with_pending_fee(db, pending_fee_cents=1, wallet_balance_cents=100_000):
    """A user with a real custodial wallet and a nonzero pending_fee_cents,
    as if a send had already happened and deducted the fee from the ledger
    without yet sweeping it."""
    private_key = "0x" + secrets.token_hex(32)
    address = Account.from_key(private_key).address

    user = User(
        email=f"feecollect_{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        full_name="Fee Collection Tester",
        is_student=True,
        crypto_wallet_address=address,
        wallet_type="fawn_custodial",
        wallet_initialized=True,
        usdc_balance_cents=wallet_balance_cents,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    ciphertext, wrapped_dek = _encrypt_private_key_envelope(private_key)
    wallet_row = CryptoWallet(
        user_id=user.id,
        wallet_address=address,
        wallet_type="fawn_custodial",
        chain="polygon",
        usdc_balance_cents=0,
        encrypted_private_key=ciphertext,
        wrapped_dek=wrapped_dek,
        key_version="v2",
        pending_fee_cents=pending_fee_cents,
    )
    db.add(wallet_row)
    db.commit()

    return user, wallet_row, private_key


@pytest.fixture(autouse=True)
def _configure_gas_station(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "gas_station_private_key", GAS_STATION_PRIVATE_KEY)


def _clear_all_pending_fees(db):
    """collect_fees() sweeps every wallet with pending_fee_cents > 0
    platform-wide -- it has no way to scope itself to "just this test".
    The shared, session-scoped test DB (see conftest.py) means earlier
    tests in this file (e.g. the send_usdc accrual tests) can leave real
    pending fees on other wallets. Tests that assert an exact/global
    collect_fees() result call this first for a clean slate; without it,
    a wallet from an earlier test -- whose RPC mocks were already
    reverted by monkeypatch's per-test teardown -- gets swept using
    whatever bm._rpc_clients happens to be at that moment, which can mean
    real network calls in a test that never intended to make any.
    """
    db.query(CryptoWallet).filter(CryptoWallet.is_treasury == False).update(  # noqa: E712
        {"pending_fee_cents": 0}, synchronize_session=False
    )
    db.commit()


# ── Envelope encryption ──

def test_legacy_encrypted_key_still_decrypts_with_no_key_version():
    # Wallets created before envelope encryption existed have key_version
    # NULL and were encrypted directly with the master key -- this must
    # keep working exactly as before, since nothing forces a migration.
    private_key = "0x" + secrets.token_hex(32)
    ciphertext = _encrypt_private_key(private_key)
    assert _decrypt_private_key(ciphertext) == private_key


def test_v2_envelope_encryption_round_trips():
    private_key = "0x" + secrets.token_hex(32)
    ciphertext, wrapped_dek = _encrypt_private_key_envelope(private_key)
    assert ciphertext != private_key.encode()
    assert wrapped_dek is not None
    decrypted = _decrypt_private_key(ciphertext, key_version="v2", wrapped_dek=wrapped_dek)
    assert decrypted == private_key


def test_v2_ciphertext_does_not_decrypt_via_legacy_path():
    # The whole point of envelope encryption: the outer ciphertext is
    # encrypted with a random per-wallet DEK, not the master key directly.
    # Decrypting it as if it were legacy-format must fail loudly, not
    # silently return garbage.
    private_key = "0x" + secrets.token_hex(32)
    ciphertext, _ = _encrypt_private_key_envelope(private_key)
    with pytest.raises(ValueError):
        crypto_wallet._decrypt_private_key_legacy(ciphertext)


def test_dek_wrap_unwrap_round_trips(monkeypatch):
    dek = Fernet.generate_key()
    kek = Fernet.generate_key()
    wrapped = _wrap_dek(dek, kek)
    assert wrapped != dek

    # _unwrap_dek reads the active KEK from FAWN_ENCRYPTION_KEY, not a
    # passed-in parameter -- point it at the same key used to wrap above.
    monkeypatch.setenv("FAWN_ENCRYPTION_KEY", kek.decode())
    monkeypatch.delenv("FAWN_ENCRYPTION_KEY_PREVIOUS", raising=False)
    assert _unwrap_dek(wrapped) == dek


def test_unwrap_dek_fails_cleanly_with_wrong_key(monkeypatch):
    dek = Fernet.generate_key()
    real_kek = Fernet.generate_key()
    wrong_kek = Fernet.generate_key()
    wrapped = _wrap_dek(dek, real_kek)

    monkeypatch.setenv("FAWN_ENCRYPTION_KEY", wrong_kek.decode())
    monkeypatch.delenv("FAWN_ENCRYPTION_KEY_PREVIOUS", raising=False)
    with pytest.raises(ValueError):
        _unwrap_dek(wrapped)


def test_key_rotation_via_previous_key_fallback(monkeypatch):
    # _active_kek/_previous_kek read FAWN_ENCRYPTION_KEY[_PREVIOUS] straight
    # from os.environ (matching the pre-existing legacy encrypt/decrypt
    # functions' own convention) -- monkeypatch.setenv/delenv auto-revert
    # after the test, so this can't leak into conftest's fixed test key.
    old_kek = Fernet.generate_key().decode()
    new_kek = Fernet.generate_key().decode()

    monkeypatch.setenv("FAWN_ENCRYPTION_KEY", old_kek)
    monkeypatch.delenv("FAWN_ENCRYPTION_KEY_PREVIOUS", raising=False)

    private_key = "0x" + secrets.token_hex(32)
    ciphertext, wrapped_dek = _encrypt_private_key_envelope(private_key)
    assert _decrypt_private_key(ciphertext, key_version="v2", wrapped_dek=wrapped_dek) == private_key

    # Rotate: new key becomes active, old key becomes the fallback.
    monkeypatch.setenv("FAWN_ENCRYPTION_KEY", new_kek)
    monkeypatch.setenv("FAWN_ENCRYPTION_KEY_PREVIOUS", old_kek)

    # Still decryptable -- falls back to the previous key.
    assert _decrypt_private_key(ciphertext, key_version="v2", wrapped_dek=wrapped_dek) == private_key

    db = SessionLocal()
    try:
        user = _make_bare_user(db)
        wallet_row = CryptoWallet(
            user_id=user.id,
            wallet_address=Account.from_key(private_key).address,
            wallet_type="fawn_custodial",
            chain="polygon",
            encrypted_private_key=ciphertext,
            wrapped_dek=wrapped_dek,
            key_version="v2",
        )
        db.add(wallet_row)
        db.commit()

        result = crypto_wallet.rotate_wallet_keys(db)
        # >= 1, not == 1 -- rotate_wallet_keys operates on every v2 wallet
        # in the DB, and other tests in this shared-session DB (see
        # conftest.py) may have already created their own v2 wallets by
        # the time this runs. What actually matters for THIS test is
        # verified below: this specific wallet's DEK really did get
        # re-wrapped under the new key (decryptable without the old one).
        assert result["rotated"] >= 1
        assert result["failed"] == 0

        db.refresh(wallet_row)
        # Now decryptable WITHOUT the previous key at all.
        monkeypatch.delenv("FAWN_ENCRYPTION_KEY_PREVIOUS", raising=False)
        assert _decrypt_private_key(wallet_row.encrypted_private_key, key_version="v2", wrapped_dek=wallet_row.wrapped_dek) == private_key
    finally:
        db.close()


# ── Treasury wallet ──

@pytest.mark.asyncio
async def test_treasury_wallet_created_once_with_seed_returned_once():
    db = SessionLocal()
    try:
        wallet1, seed1 = await crypto_wallet.get_or_create_treasury_wallet(db)
        assert wallet1.is_treasury is True
        assert wallet1.user_id is None
        assert wallet1.wallet_type == "fawn_custodial"
        assert seed1 is not None
        assert len(seed1.split()) == 12

        wallet2, seed2 = await crypto_wallet.get_or_create_treasury_wallet(db)
        assert wallet2.id == wallet1.id
        assert seed2 is None  # not shown again
    finally:
        db.close()


@pytest.mark.asyncio
async def test_treasury_wallet_creation_race_returns_the_winner(monkeypatch):
    # Simulates two concurrent first-ever calls to
    # get_or_create_treasury_wallet: a second, separate DB session creates
    # and commits a competing treasury wallet in the gap between this
    # call's own "no treasury yet" check and its own commit. The
    # idx_one_treasury_wallet partial unique index (models.py) should turn
    # that into a real IntegrityError on this call's commit, and
    # get_or_create_treasury_wallet should recover by returning the
    # winner rather than raising or leaving two treasury rows behind.
    db = SessionLocal()
    other_db = SessionLocal()
    try:
        # An earlier test in this shared-session DB (see conftest.py) may
        # already have created THE treasury wallet -- this test needs the
        # genuine "none exists yet" starting state to exercise the race,
        # so clear it first. Real production only ever hits this path
        # once, ever; this is just recreating that starting condition.
        db.query(CryptoWallet).filter(CryptoWallet.is_treasury == True).delete()  # noqa: E712
        db.commit()

        real_envelope_encrypt = crypto_wallet._encrypt_private_key_envelope
        competing = {}

        def _insert_competing_treasury_then_encrypt(private_key):
            seed2 = crypto_wallet._generate_seed_phrase()
            addr2, pk2 = crypto_wallet._derive_wallet_from_seed(seed2)
            ct2, wd2 = real_envelope_encrypt(pk2)
            other_db.add(CryptoWallet(
                user_id=None, wallet_address=addr2, wallet_type="fawn_custodial",
                chain="polygon", encrypted_private_key=ct2, wrapped_dek=wd2,
                key_version="v2", is_treasury=True,
            ))
            other_db.commit()
            competing["addr"] = addr2
            return real_envelope_encrypt(private_key)

        monkeypatch.setattr(crypto_wallet, "_encrypt_private_key_envelope", _insert_competing_treasury_then_encrypt)

        wallet, seed = await crypto_wallet.get_or_create_treasury_wallet(db)

        assert wallet.wallet_address == competing["addr"]
        assert seed is None  # this call lost the race -- no seed to reveal

        all_treasuries = db.query(CryptoWallet).filter(CryptoWallet.is_treasury == True).all()
        assert len(all_treasuries) == 1
    finally:
        db.close()
        other_db.close()


# ── pending_fee_cents accrual on send ──

@pytest.mark.asyncio
async def test_send_usdc_accrues_pending_fee_on_sender_wallet(monkeypatch):
    db = SessionLocal()
    try:
        sender, wallet_row, _ = _make_custodial_user_with_pending_fee(db, pending_fee_cents=0)
        _patch_chain(monkeypatch, "polygon", _FakeChainClient(native_usdc_cents=100_000))
        _patch_chain(monkeypatch, "base", _FakeChainClient(native_usdc_cents=0))

        result = await crypto_wallet.send_usdc(sender.id, "0x" + "1" * 40, 500, db, is_internal=True)
        assert result["status"] == "completed"

        db.refresh(wallet_row)
        assert wallet_row.pending_fee_cents == crypto_wallet.INTERNAL_TRANSFER_FEE_CENTS
    finally:
        db.close()


@pytest.mark.asyncio
async def test_send_usdc_accrues_external_fee_on_sender_wallet(monkeypatch):
    db = SessionLocal()
    try:
        sender, wallet_row, _ = _make_custodial_user_with_pending_fee(db, pending_fee_cents=0)
        _patch_chain(monkeypatch, "polygon", _FakeChainClient(native_usdc_cents=100_000))
        _patch_chain(monkeypatch, "base", _FakeChainClient(native_usdc_cents=0))

        await crypto_wallet.send_usdc(sender.id, "0x" + "1" * 40, 500, db, is_internal=False)

        db.refresh(wallet_row)
        assert wallet_row.pending_fee_cents == crypto_wallet.EXTERNAL_TRANSFER_FEE_CENTS
    finally:
        db.close()


# ── collect_fees sweep ──

@pytest.mark.asyncio
async def test_collect_fees_sweeps_pending_fee_to_treasury(monkeypatch):
    db = SessionLocal()
    try:
        _clear_all_pending_fees(db)
        user, wallet_row, private_key = _make_custodial_user_with_pending_fee(db, pending_fee_cents=50)

        # Snapshot rather than assume-empty -- other tests in this shared-
        # session DB (see conftest.py) can also insert FeeCollection rows,
        # so what's checkable is "one new row appeared for this run," not
        # "the table has exactly one row total."
        collections_before = db.query(FeeCollection).count()

        polygon_client = _FakeChainClient(native_usdc_cents=100_000, native_balance_wei=10**18)
        _patch_chain(monkeypatch, "polygon", polygon_client)
        _patch_chain(monkeypatch, "base", _FakeChainClient(native_usdc_cents=0))

        result = await crypto_wallet.collect_fees(db)

        assert result["status"] == "completed"
        assert result["total_fees"] == 50
        assert result["transfers_settled"] == 1
        assert result["failures"] == []
        # Whether treasury_seed_phrase is present depends on whether an
        # earlier test in this session already created the treasury wallet
        # (see test_treasury_wallet_created_once_with_seed_returned_once,
        # which covers the one-time-reveal contract directly) -- not
        # asserted here either way.

        db.refresh(wallet_row)
        assert wallet_row.pending_fee_cents == 0

        assert db.query(FeeCollection).count() == collections_before + 1
        latest = db.query(FeeCollection).order_by(FeeCollection.collection_date.desc()).first()
        assert latest.total_fees_cents == 50
        assert latest.treasury_wallet == result["treasury_wallet"]

        audit = db.query(UserAuditLog).filter(
            UserAuditLog.user_id == user.id,
            UserAuditLog.action == "fee_swept_to_treasury",
        ).first()
        assert audit is not None

        # The actual on-chain transfer really went to the treasury address.
        assert len(polygon_client.sent_raw_txs) == 1
        expected_raw = onchain_send._sign_transfer(
            "polygon", private_key, 0, 30_000_000_000,
            bm.CHAINS["polygon"]["contracts"]["usdc_native"], result["treasury_wallet"], 50 * 10**4,
        )
        assert polygon_client.sent_raw_txs[0] == expected_raw
    finally:
        db.close()


@pytest.mark.asyncio
async def test_collect_fees_is_noop_when_nothing_pending():
    db = SessionLocal()
    try:
        _clear_all_pending_fees(db)
        result = await crypto_wallet.collect_fees(db)
        assert result["status"] == "noop"
        assert result["total_fees"] == 0
        assert result["transfers_settled"] == 0
    finally:
        db.close()


@pytest.mark.asyncio
async def test_collect_fees_partial_failure_does_not_block_other_wallets(monkeypatch):
    db = SessionLocal()
    try:
        _clear_all_pending_fees(db)
        good_user, good_wallet, good_key = _make_custodial_user_with_pending_fee(db, pending_fee_cents=10)
        bad_user, bad_wallet, _ = _make_custodial_user_with_pending_fee(db, pending_fee_cents=20)

        _patch_chain(monkeypatch, "polygon", _FakeChainClient(native_usdc_cents=100_000, native_balance_wei=10**18))
        _patch_chain(monkeypatch, "base", _FakeChainClient(native_usdc_cents=0))

        real_sweep = onchain_send.sweep_wallet_fee

        async def _flaky_sweep(wallet, treasury_address, amount_cents, db):
            if wallet.wallet_address == bad_wallet.wallet_address:
                raise onchain_send.OnchainSendFailed("simulated RPC failure")
            return await real_sweep(wallet, treasury_address, amount_cents, db)

        monkeypatch.setattr(onchain_send, "sweep_wallet_fee", _flaky_sweep)

        result = await crypto_wallet.collect_fees(db)

        assert result["status"] == "partial"
        assert result["total_fees"] == 10
        assert result["transfers_settled"] == 1
        assert len(result["failures"]) == 1
        assert result["failures"][0]["wallet_address"] == bad_wallet.wallet_address

        db.refresh(good_wallet)
        db.refresh(bad_wallet)
        assert good_wallet.pending_fee_cents == 0
        assert bad_wallet.pending_fee_cents == 20  # untouched, will retry next run
    finally:
        db.close()


def test_atomic_fee_claim_prevents_double_claim():
    """collect_fees() protects against two overlapping runs (the daily
    scheduler racing a manual admin call, say) both sweeping the same
    wallet's fee by atomically claiming it first: zero pending_fee_cents,
    but only if it still matches the value just read. This directly
    exercises that same conditional-UPDATE primitive standalone --
    genuinely racing two real concurrent collect_fees() calls against
    SQLite in a test is unreliable (file-level locking tends to just
    serialize them), so this proves the underlying safety mechanism
    instead of trying to force a flaky real race."""
    db = SessionLocal()
    try:
        user, wallet_row, _ = _make_custodial_user_with_pending_fee(db, pending_fee_cents=50)
        fee_amount = wallet_row.pending_fee_cents

        first_claim = db.query(CryptoWallet).filter(
            CryptoWallet.id == wallet_row.id,
            CryptoWallet.pending_fee_cents == fee_amount,
        ).update({"pending_fee_cents": 0}, synchronize_session=False)
        db.commit()
        assert first_claim == 1

        # A second attempt reading the same now-stale fee_amount must find
        # nothing left to claim -- this is what stops a double-sweep.
        second_claim = db.query(CryptoWallet).filter(
            CryptoWallet.id == wallet_row.id,
            CryptoWallet.pending_fee_cents == fee_amount,
        ).update({"pending_fee_cents": 0}, synchronize_session=False)
        db.commit()
        assert second_claim == 0
    finally:
        db.close()


@pytest.mark.asyncio
async def test_collect_fees_restores_pending_fee_additively_on_failure(monkeypatch):
    db = SessionLocal()
    try:
        _clear_all_pending_fees(db)
        user, wallet_row, _ = _make_custodial_user_with_pending_fee(db, pending_fee_cents=30)

        _patch_chain(monkeypatch, "polygon", _FakeChainClient(native_usdc_cents=100_000, native_balance_wei=10**18))
        _patch_chain(monkeypatch, "base", _FakeChainClient(native_usdc_cents=0))

        async def _fails_after_a_new_send_lands(wallet, treasury_address, amount_cents, db):
            # Simulate a NEW send landing (growing pending_fee_cents again)
            # while this sweep attempt is in flight, then the sweep itself
            # fails -- the restore-on-failure must not clobber that growth.
            db.query(CryptoWallet).filter(CryptoWallet.id == wallet.id).update(
                {"pending_fee_cents": CryptoWallet.pending_fee_cents + 5}, synchronize_session=False
            )
            db.commit()
            raise onchain_send.OnchainSendFailed("simulated failure mid-flight")

        monkeypatch.setattr(onchain_send, "sweep_wallet_fee", _fails_after_a_new_send_lands)

        result = await crypto_wallet.collect_fees(db)

        assert result["status"] == "partial"
        assert result["failures"][0]["wallet_address"] == wallet_row.wallet_address

        db.refresh(wallet_row)
        # 30 claimed-then-restored + 5 landed mid-flight = 35 -- not a
        # blind overwrite back to 30 that would silently drop the 5.
        assert wallet_row.pending_fee_cents == 35
    finally:
        db.close()


@pytest.mark.asyncio
async def test_collect_fees_ignores_treasury_wallet_itself(monkeypatch):
    # The treasury wallet should never try to sweep a fee to itself.
    db = SessionLocal()
    try:
        _clear_all_pending_fees(db)
        # Defense in depth even after the cleanup above: if this ever DID
        # find something to sweep, it must not be a real network call.
        _patch_chain(monkeypatch, "polygon", _FakeChainClient(native_usdc_cents=100_000, native_balance_wei=10**18))
        _patch_chain(monkeypatch, "base", _FakeChainClient(native_usdc_cents=0))

        treasury, _ = await crypto_wallet.get_or_create_treasury_wallet(db)
        treasury.pending_fee_cents = 999  # shouldn't happen in practice, but must not be swept if it does
        db.commit()

        result = await crypto_wallet.collect_fees(db)
        assert result["status"] == "noop"
    finally:
        # Don't leave the treasury wallet's pending_fee_cents corrupted
        # for whatever runs next in this shared-session DB.
        db.query(CryptoWallet).filter(CryptoWallet.is_treasury == True).update(  # noqa: E712
            {"pending_fee_cents": 0}, synchronize_session=False
        )
        db.commit()
        db.close()


# ── Router-level custodial-only enforcement ──

def test_non_custodial_wallet_creation_is_rejected_at_router(client):
    register_resp = client.post("/auth/register", json={
        "email": f"feecollect_{uuid.uuid4().hex[:8]}@example.com",
        "password": "supersecret1",
        "full_name": "Router Tester",
        "phone": "5551234567",
        "is_student": True,
        "school": "berkeley",
        "location": "Berkeley, CA",
        "military_status": "none",
    })
    assert register_resp.status_code == 201, register_resp.text
    token = register_resp.json()["access_token"]

    r = client.post(
        "/wallet/create",
        json={"wallet_type": "non_custodial"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400
    assert "custodial" in r.json()["detail"].lower()


def test_custodial_wallet_creation_still_works_at_router(client):
    # The default wallet_type (no body at all) must still succeed --
    # confirms the non_custodial check didn't accidentally block the one
    # wallet type that's actually offered.
    register_resp = client.post("/auth/register", json={
        "email": f"feecollect_{uuid.uuid4().hex[:8]}@example.com",
        "password": "supersecret1",
        "full_name": "Router Tester 2",
        "phone": "5551234567",
        "is_student": True,
        "school": "berkeley",
        "location": "Berkeley, CA",
        "military_status": "none",
    })
    assert register_resp.status_code == 201, register_resp.text
    token = register_resp.json()["access_token"]

    r = client.post(
        "/wallet/create",
        json={},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["wallet_type"] == "fawn_custodial"


# ── POST /fees/collect over real HTTP ──
#
# Every other collect_fees() test in this file calls
# crypto_wallet.collect_fees(db) directly -- none of them exercise the
# admin-key auth gate or the actual JSON FastAPI produces from the raw
# dict (routers/crypto.py has no response_model, so nothing strips
# fields -- including the sensitive one-time treasury_seed_phrase). These
# close that gap.

def test_fees_collect_requires_admin_key(client):
    r = client.post("/fees/collect")
    assert r.status_code == 401


def test_fees_collect_rejects_wrong_admin_key(client):
    r = client.post("/fees/collect", headers={"X-Admin-Key": "definitely-not-the-real-key"})
    assert r.status_code == 401


def test_fees_collect_via_http_sweeps_a_real_pending_fee(client, admin_key, monkeypatch):
    db = SessionLocal()
    try:
        _clear_all_pending_fees(db)
        _make_custodial_user_with_pending_fee(db, pending_fee_cents=25)
    finally:
        db.close()

    _patch_chain(monkeypatch, "polygon", _FakeChainClient(native_usdc_cents=100_000, native_balance_wei=10**18))
    _patch_chain(monkeypatch, "base", _FakeChainClient(native_usdc_cents=0))

    r = client.post("/fees/collect", headers={"X-Admin-Key": admin_key})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_fees"] >= 25
    assert body["status"] in ("completed", "partial")
    assert body["treasury_wallet"].startswith("0x")


def test_fees_collect_via_http_returns_seed_phrase_when_treasury_is_new(client, admin_key, monkeypatch):
    db = SessionLocal()
    try:
        _clear_all_pending_fees(db)
        # Force a genuinely new treasury creation through this specific
        # HTTP call, so the one-time seed-phrase field is guaranteed to
        # actually be exercised here rather than depending on whichever
        # earlier test happened to create the treasury wallet first.
        db.query(CryptoWallet).filter(CryptoWallet.is_treasury == True).delete()  # noqa: E712
        db.commit()
        _make_custodial_user_with_pending_fee(db, pending_fee_cents=15)
    finally:
        db.close()

    _patch_chain(monkeypatch, "polygon", _FakeChainClient(native_usdc_cents=100_000, native_balance_wei=10**18))
    _patch_chain(monkeypatch, "base", _FakeChainClient(native_usdc_cents=0))

    r = client.post("/fees/collect", headers={"X-Admin-Key": admin_key})
    assert r.status_code == 200, r.text
    body = r.json()
    # Confirms the sensitive one-time seed phrase actually round-trips
    # through the real HTTP response instead of getting silently dropped.
    assert body.get("treasury_seed_phrase") is not None
    assert len(body["treasury_seed_phrase"].split()) == 12
