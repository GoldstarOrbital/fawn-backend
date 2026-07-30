# FAWN HCE protocol v1

FAWN HCE is a closed-loop Android contactless protocol. It is not EMV, Visa,
Mastercard, Apple Pay, or Google Pay. The Android service registers the
proprietary ISO/IEC 7816 application identifier `F04641574E0101` in the
`other` category and requires the customer device to be unlocked.

## Security invariants

- The Android private key is generated as P-256 inside Android Keystore and is
  never returned to JavaScript or the backend.
- Device registration accepts only DER SubjectPublicKeyInfo P-256 public keys.
- Every challenge is 32 random bytes, expires after 30 seconds, and is bound in
  the database to one checkout and one approved merchant.
- The phone signs `FAWN-NFC-v1 || 0x00 || challenge` using ECDSA with SHA-256.
- The merchant cannot reuse the signature for another checkout because the
  challenge row is checkout-bound and consumed during the settlement lock.
- NFC checkout is capped at $100 before normal card per-transaction and rolling
  24-hour limits are applied.
- The ordinary FAWN settlement function remains the only code allowed to debit
  the payer, credit the merchant, and book the exact 1-cent fees.

## APDU exchange

1. Reader selects AID `F04641574E0101` with `00 A4 04 00`.
2. Phone returns `90 00`.
3. Reader sends challenge command `80 10 00 00 20 <32 bytes> 00`.
4. Phone returns:
   - protocol version: 1 byte (`01`)
   - device UUID: 16 bytes, network byte order
   - DER signature length: 2 bytes, unsigned network byte order
   - DER ECDSA signature: 64 to 80 bytes
   - status: `90 00`
5. Merchant submits the device UUID, original challenge, and signature to the
   checkout's NFC authorization endpoint.

## Backend endpoints

- `POST /closed-loop/cards/me/nfc-devices`
- `GET /closed-loop/cards/me/nfc-devices`
- `DELETE /closed-loop/cards/me/nfc-devices/{device_id}`
- `POST /closed-loop/merchant/checkouts/{checkout_token}/nfc-challenge`
- `POST /closed-loop/merchant/checkouts/{checkout_token}/nfc-authorize`

## Production gate

The protocol is enabled only in a native FAWN Android build; Expo Go and web
browsers cannot load the HCE module. A two-device physical test must validate
device unlock behavior, APDU timing, reader cancellation, signature replay,
and settlement before FAWN describes Android HCE as generally available.
