# FAWN card program boundaries

FAWN supports a closed-loop virtual balance card for FAWN-controlled merchant
checkout. It does not issue a Visa/Mastercard PAN, CVV, or card-network token.

## Closed-loop path

1. A custodial-wallet user issues one `ClosedLoopCard`.
2. A merchant applies, links a FAWN account, and remains `pending_review`
   until an administrator approves it.
3. An active merchant creates a 15-minute checkout for an exact cent amount.
4. The customer authorizes in the FAWN web/mobile experience, or creates a
   60-second, single-use dynamic tap token bound to that checkout and merchant
   for a FAWN terminal.
5. Settlement row-locks the checkout, card, payer, merchant, and mirrored
   wallet records. It debits purchase + 1 cent from the customer and credits
   purchase - 1 cent to the merchant. Both one-cent fees are immutable on the
   checkout record.
6. Audit records for both sides are retained for seven years.

The checkout token makes capture idempotent. A completed checkout cannot be
paid a second time. Dynamic tap tokens are stored only as SHA-256 hashes and
are invalid after first use or 60 seconds. A token created for one checkout
cannot authorize a different amount or merchant.

## Phone-wallet and tap boundary

The FAWN mobile app is the phone wallet for this closed loop. It can display a
rotating QR/tap payload that a FAWN terminal submits to the API. This protocol
is ready for a future Android Host Card Emulation or approved NFC reader
integration, but Expo/web code alone does not make a phone emulate a payment
card.

Apple Pay and Google Pay payment-card provisioning remain false because a
closed-loop credential is not a card-network payment card. Apple Wallet NFC,
Google Wallet Smart Tap, Android HCE, and Tap to Pay on iPhone each have
separate platform approvals or native requirements; the UI must not represent
the current QR/dynamic-token path as those products.

## Network-card gate

General-purpose card issuance can only be enabled after FAWN has the legally
appropriate issuing structure, network agreement, PCI-scoped card lifecycle,
Apple/Google approvals, and tested native provisioning. No third-party issuer
credential silently enables that path.
