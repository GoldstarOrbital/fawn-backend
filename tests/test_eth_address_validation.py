"""Regression tests for services/crypto_wallet.py::_is_valid_eth_address.

Found while adding the treasury wallet (tests/test_fee_collection.py): the
EIP-55 checksum branch indexed into hash *bytes* with `hash_bytes[i // 2]`
(an int, not a byte-length-1 bytes object) and then called `.hex()` on
that int -- AttributeError on any real address whose checksum loop
actually reaches that line. Existing tests never hit it because their
addresses were all-digit ("0x" + "1"*40) or all-repeated-lowercase-letter
("0x" + "ab"*20), both of which skip the checksum loop body entirely. It
also hashed with SHA-256 instead of the Keccak-256 EIP-55 actually
specifies, so even a non-crashing path would have validated against the
wrong bytes. Both are fixed; this file is the regression coverage that
was missing.
"""
import secrets

from eth_account import Account

from services.crypto_wallet import _is_valid_eth_address


def test_real_checksummed_addresses_validate_without_crashing():
    # Ten fresh, real, algorithmically-checksummed addresses (mixed case
    # is the norm) -- this is exactly the class of input that used to
    # raise AttributeError instead of returning True/False.
    for _ in range(10):
        private_key = "0x" + secrets.token_hex(32)
        address = Account.from_key(private_key).address
        assert _is_valid_eth_address(address) is True


def test_corrupted_checksum_case_is_rejected():
    # Proves the checksum logic actually discriminates, not just "returns
    # True for everything real-looking" -- flip the case of one letter in
    # an otherwise-valid checksummed address and confirm it's now invalid.
    private_key = "0x" + secrets.token_hex(32)
    address = Account.from_key(private_key).address

    body = list(address[2:])
    flipped = False
    for i, c in enumerate(body):
        if c.isalpha():
            body[i] = c.lower() if c.isupper() else c.upper()
            flipped = True
            break
    assert flipped, "expected at least one letter in a real checksummed address"

    corrupted = "0x" + "".join(body)
    assert corrupted != address
    assert _is_valid_eth_address(corrupted) is False


def test_all_lowercase_and_all_uppercase_are_accepted_as_unchecksummed():
    private_key = "0x" + secrets.token_hex(32)
    address = Account.from_key(private_key).address
    assert _is_valid_eth_address("0x" + address[2:].lower()) is True
    assert _is_valid_eth_address("0x" + address[2:].upper()) is True


def test_malformed_addresses_are_rejected():
    assert _is_valid_eth_address("") is False
    assert _is_valid_eth_address("not an address") is False
    assert _is_valid_eth_address("0x" + "1" * 39) is False  # too short
    assert _is_valid_eth_address("0x" + "1" * 41) is False  # too long
    assert _is_valid_eth_address("0x" + "g" * 40) is False  # invalid hex chars
    assert _is_valid_eth_address("0x" + "1" * 40) is True  # all-digit, valid format


def test_regression_treasury_style_address_does_not_crash():
    # The exact failure mode that surfaced this bug: a real derived
    # treasury/custodial wallet address must validate cleanly.
    from services.crypto_wallet import _derive_wallet_from_seed, _generate_seed_phrase

    seed = _generate_seed_phrase()
    address, _ = _derive_wallet_from_seed(seed)
    assert _is_valid_eth_address(address) is True
