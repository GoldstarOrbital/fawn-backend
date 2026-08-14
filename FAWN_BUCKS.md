# Fawn Bucks

Fawn Bucks are off-chain, non-transferable, non-redeemable Fawn service credits. They are not a blockchain token, wallet, security, or peer-to-peer payment instrument.

## Configuration

Set `STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET` in the backend deployment. Configure the Stripe webhook URL as `<public-api-base>/stripe/webhook` for `checkout.session.completed`, `charge.refunded`, and `charge.dispute.created`. No client callback issues credit.

Run the Alembic migration before enabling checkout:

```text
alembic upgrade head
```

The checkout accepts whole-dollar amounts. The server calculates a fee of one cent per dollar, includes the fee in the Stripe total, and stores both values in metadata. A paid webhook creates one immutable serial row per whole Buck and an append-only issuance ledger entry. Refunds and disputes append reversal entries and mark active serials reversed. Authenticated history is filtered by the JWT user.

Stripe credentials and webhook secrets are intentionally not included in source control.
