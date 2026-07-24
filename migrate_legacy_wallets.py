"""Reconcile and migrate legacy non-custodial wallets.

Safe by default: without ``--apply`` this command performs a read-only dry run.
The apply path is intentionally explicit and idempotent. It never deletes a
wallet or ledger/audit row; it deactivates the legacy row, provisions a new
encrypted custodial wallet, and moves only a reconciled internal balance.

Run after the release containing the wallet lifecycle columns is online:

    python migrate_legacy_wallets.py
    python migrate_legacy_wallets.py --apply \
      --confirm MIGRATE_LEGACY_WALLETS_2026
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timedelta, timezone

from database import SessionLocal
from models import CryptoWallet, User, UserAuditLog, new_id
from sqlalchemy import or_
from services import crypto_wallet
from services.onchain_send import _get_native_usdc_balance


CONFIRMATION = "MIGRATE_LEGACY_WALLETS_2026"


def _legacy_rows(db):
    """Return every legacy *user*, including records with a missing wallet row.

    Older releases could leave wallet state only on ``users`` or point a user
    at a wallet row without an encrypted signing key. The previous migration
    joined only active non-custodial ``CryptoWallet`` rows, silently skipping
    both situations and leaving the old address and ledger visible in the UI.
    """
    candidates = (
        db.query(User)
        .filter(or_(
            User.wallet_initialized.is_(True),
            User.crypto_wallet_address.isnot(None),
            User.wallet_type == "non_custodial",
        ))
        .order_by(User.created_at, User.id)
        .all()
    )
    rows = []
    for user in candidates:
        wallets = db.query(CryptoWallet).filter(
            CryptoWallet.is_treasury.is_(False),
            or_(
                CryptoWallet.user_id == user.id,
                CryptoWallet.wallet_address.ilike(user.crypto_wallet_address) if user.crypto_wallet_address else False,
            ),
        ).order_by(CryptoWallet.status.desc(), CryptoWallet.created_at.desc()).all()
        active_custodial = next((w for w in wallets if w.status == "active" and w.wallet_type == "fawn_custodial" and w.encrypted_private_key and w.wallet_address == user.crypto_wallet_address), None)
        if active_custodial:
            continue
        # Prefer the active row, but retain an inactive/address-matched row in
        # the report so its balance and chain are still reconciled.
        legacy = next((w for w in wallets if w.status == "active"), wallets[0] if wallets else None)
        rows.append((legacy, user))
    return rows


async def _reconcile(wallet: CryptoWallet | None, user: User) -> dict:
    issues: list[str] = []
    wallet_balance = int(wallet.usdc_balance_cents or 0) if wallet else int(user.usdc_balance_cents or 0)
    user_balance = int(user.usdc_balance_cents or 0)
    if wallet and wallet_balance != user_balance:
        issues.append(f"ledger mismatch wallet={wallet_balance} user={user_balance}")
    if wallet and wallet.user_id not in (None, user.id):
        issues.append("user-to-wallet ownership mismatch")

    old_address = user.crypto_wallet_address or (wallet.wallet_address if wallet else None)
    chain = wallet.chain if wallet else "polygon"
    if old_address:
        onchain = await _get_native_usdc_balance(chain, old_address)
        if onchain is None:
            issues.append("on-chain USDC balance could not be read")
        elif onchain != 0:
            issues.append(f"on-chain USDC balance is {onchain} cents; manual reconciliation required")
    else:
        onchain = 0

    return {
        "user_id": user.id,
        "wallet_id": wallet.id if wallet else None,
        "wallet_address": old_address,
        "chain": chain,
        "ledger_balance_cents": wallet_balance,
        "user_balance_cents": user_balance,
        "pending_fee_cents": int(wallet.pending_fee_cents or 0) if wallet else 0,
        "onchain_balance_cents": onchain,
        "issues": issues,
        "ready": not issues,
    }


def _new_custodial_wallet(user: User, chain: str, balance: int, pending_fee: int) -> CryptoWallet:
    seed_phrase = crypto_wallet._generate_seed_phrase()
    address, private_key = crypto_wallet._derive_wallet_from_seed(seed_phrase)
    encrypted_key, wrapped_dek = crypto_wallet._encrypt_private_key_envelope(private_key)
    if crypto_wallet._decrypt_private_key(encrypted_key, "v2", wrapped_dek) != private_key:
        raise RuntimeError("custodial key round-trip verification failed")
    return CryptoWallet(
        id=new_id(),
        user_id=user.id,
        wallet_address=address,
        wallet_type="fawn_custodial",
        chain=chain,
        usdc_balance_cents=balance,
        pending_fee_cents=pending_fee,
        encrypted_private_key=encrypted_key,
        wrapped_dek=wrapped_dek,
        key_version="v2",
        status="active",
        is_treasury=False,
    )


async def main(apply: bool, confirmation: str | None) -> int:
    db = SessionLocal()
    try:
        rows = _legacy_rows(db)
        report = []
        for legacy, user in rows:
            report.append(await _reconcile(legacy, user))

        print(json.dumps({
            "mode": "apply" if apply else "dry-run",
            "legacy_wallets": len(report),
            "ready": sum(1 for row in report if row["ready"]),
            "blocked": sum(1 for row in report if not row["ready"]),
            "wallets": report,
        }, indent=2, sort_keys=True))

        if not apply:
            return 0
        if confirmation != CONFIRMATION:
            raise SystemExit(f"Refusing apply: pass --confirm {CONFIRMATION}")
        blocked = [row for row in report if not row["ready"]]
        if blocked:
            raise SystemExit("Refusing apply: reconciliation has unresolved wallets")

        migrated = 0
        for legacy, user in _legacy_rows(db):
            # Idempotency guard if a previous run completed this user.
            balance = int(legacy.usdc_balance_cents or 0) if legacy else int(user.usdc_balance_cents or 0)
            pending_fee = int(legacy.pending_fee_cents or 0) if legacy else 0
            replacement = _new_custodial_wallet(user, legacy.chain if legacy else "polygon", balance, pending_fee)
            old_address = user.crypto_wallet_address or (legacy.wallet_address if legacy else None)
            old_id = legacy.id if legacy else None

            # The old row remains as an immutable migration reference, but is
            # detached from the one-active-wallet user relationship.
            if legacy:
                legacy.user_id = None
                legacy.status = "inactive"
                legacy.superseded_by = replacement.id
                legacy.deactivated_at = datetime.now(timezone.utc)
                legacy.deactivation_reason = "replaced_by_custodial_wallet"
                legacy.usdc_balance_cents = 0
                legacy.pending_fee_cents = 0

            db.add(replacement)
            user.crypto_wallet_address = replacement.wallet_address
            user.wallet_type = "fawn_custodial"
            user.wallet_initialized = True
            user.usdc_balance_cents = balance
            audit = UserAuditLog(
                user_id=user.id,
                action="migrated_wallet_to_custodial",
                details=json.dumps({
                    "legacy_wallet_id": old_id,
                    "legacy_wallet_address": old_address,
                    "replacement_wallet_id": replacement.id,
                    "replacement_wallet_address": replacement.wallet_address,
                    "reconciled_ledger_balance_cents": balance,
                    "reconciled_onchain_balance_cents": 0,
                    "migrated_pending_fee_cents": pending_fee,
                }),
                retention_expires_at=datetime.now(timezone.utc) + timedelta(days=365 * 7),
            )
            db.add(audit)
            db.commit()
            migrated += 1

        print(json.dumps({"migrated": migrated, "status": "complete"}, indent=2))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="apply only after a clean dry run")
    parser.add_argument("--confirm", help="required confirmation token for --apply")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.apply, args.confirm)))
