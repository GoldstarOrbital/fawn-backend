# Wallet Creation - Code Implementation Reference

## File: services/crypto_wallet.py

### Import Additions

```python
# NEW IMPORTS ADDED (at top of file)

# HD wallet derivation from BIP39 seed
try:
    from eth_account import Account
    from eth_utils import to_checksum_address
except ImportError:
    Account = None
    to_checksum_address = None
```

### New Function: _derive_wallet_from_seed()

```python
def _derive_wallet_from_seed(seed_phrase: str) -> tuple[str, str]:
    """
    Derive Ethereum wallet address and private key from BIP39 seed phrase.

    Uses standard HD derivation path m/44'/60'/0'/0/0 (first Ethereum account).

    Args:
        seed_phrase: 12-word BIP39 seed phrase (space-separated)

    Returns:
        (wallet_address, private_key_hex) where private_key_hex includes "0x" prefix

    Raises:
        ImportError if eth-account not installed
        ValueError if seed phrase is invalid
    """
    if Account is None or to_checksum_address is None:
        raise ImportError(
            "eth-account and eth-utils not installed. "
            "Install with: pip install eth-account eth-keys eth-utils"
        )

    try:
        # Derive account from seed phrase using standard path
        account = Account.from_mnemonic(seed_phrase, account_path=BIP39_DERIVATION_PATH)
        address = to_checksum_address(account.address)  # EIP-55 checksummed
        private_key = account.key.hex()  # Returns 0x-prefixed hex string

        return address, private_key
    except Exception as e:
        raise ValueError(f"Failed to derive wallet from seed phrase: {e}")
```

### Updated: _encrypt_private_key()

**BEFORE:**
```python
def _encrypt_private_key(private_key: str, encryption_key: Optional[str] = None) -> bytes:
    """Encrypt private key using Fernet (AES-256-GCM)."""
    if Fernet is None:
        raise ImportError("cryptography library not installed. Install with: pip install cryptography")

    # Use environment key or generate a new one (not production-safe!)
    key = encryption_key or os.environ.get("FAWN_ENCRYPTION_KEY")
    if not key:
        raise ValueError("FAWN_ENCRYPTION_KEY environment variable not set")

    # Ensure key is properly formatted for Fernet (base64-encoded 32 bytes)
    if isinstance(key, str):
        key = key.encode()

    cipher = Fernet(key)
    return cipher.encrypt(private_key.encode())
```

**AFTER:**
```python
def _encrypt_private_key(private_key: str, encryption_key: Optional[str] = None) -> bytes:
    """
    Encrypt private key using Fernet (AES-256-GCM).

    Args:
        private_key: hex-encoded private key (with or without 0x prefix)
        encryption_key: optional encryption key; defaults to FAWN_ENCRYPTION_KEY env var

    Returns:
        bytes: Fernet-encrypted ciphertext (includes IV + tag)

    Raises:
        ImportError if cryptography not installed
        ValueError if no encryption key available
    """
    if Fernet is None:
        raise ImportError("cryptography library not installed. Install with: pip install cryptography")

    # Use environment key or raise error
    key = encryption_key or os.environ.get("FAWN_ENCRYPTION_KEY")
    if not key:
        raise ValueError("FAWN_ENCRYPTION_KEY environment variable not set")

    # Ensure key is properly formatted for Fernet (base64-encoded 32 bytes)
    if isinstance(key, str):
        key = key.encode()

    try:
        cipher = Fernet(key)
        return cipher.encrypt(private_key.encode())
    except Exception as e:
        raise ValueError(f"Encryption failed: {e}")
```

### Updated: create_wallet()

**BEFORE (Key sections):**
```python
async def create_wallet(user_id: str, db: Session, wallet_type: str = "fawn_custodial") -> dict:
    # ...validation...

    # Generate wallet address (MVP: placeholder)
    wallet_address = f"0x{secrets.token_hex(20)}"  # Random hex, NOT real derivation

    # Generate and store encryption key for custodial wallets
    seed_phrase = None
    encrypted_key = None

    if wallet_type == "non_custodial":
        seed_phrase = _generate_seed_phrase()  # Generate but don't store
    elif wallet_type == "fawn_custodial":
        seed_phrase = _generate_seed_phrase()
        encrypted_key = _encrypt_private_key(seed_phrase)  # BUG: encrypting seed, not private key

    wallet = CryptoWallet(
        user_id=user_id,
        wallet_address=wallet_address,
        wallet_type=wallet_type,
        chain=USDC_CHAIN,
        usdc_balance_cents=0,
        # encrypted_private_key=encrypted_key,  # TODO: uncomment once schema is fixed
    )
```

**AFTER (Key sections):**
```python
async def create_wallet(user_id: str, db: Session, wallet_type: str = "fawn_custodial") -> dict:
    # ...validation...

    # Generate BIP39 seed phrase (12 words)
    seed_phrase = _generate_seed_phrase()

    # Derive wallet address and private key from seed (REAL DERIVATION)
    wallet_address, private_key_hex = _derive_wallet_from_seed(seed_phrase)

    # Encrypt private key for storage (custodial only)
    encrypted_key = None
    if wallet_type == "fawn_custodial":
        encrypted_key = _encrypt_private_key(private_key_hex)  # Encrypt private key, not seed

    # Create wallet record in database
    wallet = CryptoWallet(
        user_id=user_id,
        wallet_address=wallet_address,
        wallet_type=wallet_type,
        chain=USDC_CHAIN,
        usdc_balance_cents=0,
        encrypted_private_key=encrypted_key,  # Now properly stored
    )
    db.add(wallet)

    # ...rest of function (user update, audit log)...

    # SECURITY: Return seed phrase ONLY for non-custodial wallets
    return {
        "wallet_address": wallet_address,
        "wallet_type": wallet_type,
        "usdc_balance": 0.0,
        "chain": USDC_CHAIN,
        "seed_phrase": seed_phrase if wallet_type == "non_custodial" else None,
    }
```

### Constants

```python
# Add at top of file after imports
BIP39_DERIVATION_PATH = "m/44'/60'/0'/0/0"  # Standard Ethereum account (BIP44)
```

---

## File: requirements.txt

### Changes

```diff
  mnemonic>=0.20
  cryptography>=42.0.0
  stripe>=10.0.0
+ eth-account>=0.10.0
+ eth-utils>=2.0.0
+ eth-keys>=0.4.0
```

---

## File: routers/crypto.py

### NO CHANGES NEEDED

The endpoint is already correctly implemented:

```python
@router.post("/create", response_model=CreateWalletResponse, status_code=201)
@limiter.limit(RATE_LIMITS["wallet_create"])
async def create_wallet(
    req: CreateWalletRequest,
    request: Request,
    user_id: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """
    Create a new stablecoin wallet for the logged-in user.

    SECURITY: Only one wallet per user. Idempotent.
    - non_custodial: User manages private key (seed phrase returned — must save, SHOWN ONCE ONLY)
    - fawn_custodial: FAWN holds encrypted key (user accesses via PIN — MVP)

    Returns wallet address and (for non-custodial only) the seed phrase.
    CRITICAL: Seed phrase is shown ONCE and cannot be recovered if lost.
    """
    try:
        result = await crypto_wallet.create_wallet(user_id, db, wallet_type=req.wallet_type)
        capture(EVENTS["WALLET_CREATED"], user_id, {"wallet_type": req.wallet_type})
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Wallet error: {str(e)[:100]}")
```

---

## File: models.py

### NO CHANGES NEEDED

The schema already supports everything:

```python
class CryptoWallet(Base):
    """Stablecoin wallet per user — tracks address, balance, and metadata."""
    __tablename__ = "crypto_wallets"

    id = Column(String, primary_key=True, default=new_id)
    user_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, unique=True, index=True)
    wallet_address = Column(String, nullable=False, unique=True, index=True)
    wallet_type = Column(String, nullable=False)  # "non_custodial" | "fawn_custodial"
    chain = Column(String, nullable=False, default="polygon")  # "polygon" | "ethereum"
    usdc_balance_cents = Column(Integer, default=0, nullable=False)
    encrypted_private_key = Column(LargeBinary, nullable=True)  # ← Already here
    created_at = Column(DateTime(timezone=True), server_default=func.now())
```

---

## File: schemas.py

### NO CHANGES NEEDED

Wallet fields already in UserResponse:

```python
class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    full_name: str
    is_student: bool
    school: Optional[str] = None
    location: Optional[str] = None
    military_status: Optional[str] = None
    wallet_initialized: Optional[bool] = None
    crypto_wallet_address: Optional[str] = None
    wallet_type: Optional[str] = None
    # ...
```

---

## Example: Creating a Non-Custodial Wallet

### Input

```bash
curl -X POST http://localhost:8000/wallet/create \
  -H "Authorization: Bearer eyJ0eXAiOiJKV1QiLCJhbGc..." \
  -H "Content-Type: application/json" \
  -d '{"wallet_type": "non_custodial"}'
```

### Processing

```
1. Generate seed phrase (12 BIP39 words)
   seed = "abandon ability able about above absent absorb abstract abuse access accident account"

2. Derive HD wallet from seed (m/44'/60'/0'/0/0)
   address = "0x742d35Cc6634C0532925a3b844Bc2e7a1b8Bef6A"  (EIP-55 checksummed)
   private_key = "0x1234567890abcdef..."

3. For non_custodial: Don't store key
   encrypted_key = None

4. Create CryptoWallet record
   user_id = "user-123"
   wallet_address = "0x742d35Cc6634C0532925a3b844Bc2e7a1b8Bef6A"
   wallet_type = "non_custodial"
   encrypted_private_key = None

5. Update User record
   crypto_wallet_address = "0x742d35Cc6634C0532925a3b844Bc2e7a1b8Bef6A"
   wallet_type = "non_custodial"
   wallet_initialized = True

6. Audit log (7-year retention)
   action = "created_wallet"
   details = {
     "wallet_type": "non_custodial",
     "chain": "polygon",
     "wallet_address": "0x742d35Cc6634C0532925a3b844Bc2e7a1b8Bef6A"
   }

7. Return to user (SEED SHOWN ONCE)
   seed_phrase = "abandon ability able about above absent absorb abstract abuse access accident account"
```

### Output

```json
{
  "wallet_address": "0x742d35Cc6634C0532925a3b844Bc2e7a1b8Bef6A",
  "wallet_type": "non_custodial",
  "usdc_balance": 0.0,
  "chain": "polygon",
  "seed_phrase": "abandon ability able about above absent absorb abstract abuse access accident account"
}
```

---

## Example: Creating a Custodial Wallet

### Input

```bash
curl -X POST http://localhost:8000/wallet/create \
  -H "Authorization: Bearer eyJ0eXAiOiJKV1QiLCJhbGc..." \
  -H "Content-Type: application/json" \
  -d '{"wallet_type": "fawn_custodial"}'
```

### Processing

```
1. Generate seed phrase (12 BIP39 words) [INTERNAL ONLY]
   seed = "abandon ability able about above absent absorb abstract abuse access accident account"

2. Derive HD wallet from seed
   address = "0x742d35Cc6634C0532925a3b844Bc2e7a1b8Bef6A"
   private_key = "0x1234567890abcdef..."

3. For fawn_custodial: ENCRYPT the private key
   key = os.environ["FAWN_ENCRYPTION_KEY"]  # Base64-encoded 32 bytes
   cipher = Fernet(key)
   encrypted_key = cipher.encrypt(private_key.encode())
   # Result: bytes like b'gAAAAABmh...' (IV + ciphertext + tag)

4. Create CryptoWallet record
   encrypted_private_key = encrypted_key  # Stored in database

5. Audit log
   # Does NOT include seed_phrase or private_key

6. Return to user (NO SEED EXPOSED)
   seed_phrase = None
```

### Output

```json
{
  "wallet_address": "0x742d35Cc6634C0532925a3b844Bc2e7a1b8Bef6A",
  "wallet_type": "fawn_custodial",
  "usdc_balance": 0.0,
  "chain": "polygon",
  "seed_phrase": null
}
```

---

## Example: /auth/me Response After Wallet Creation

```bash
curl -H "Authorization: Bearer eyJ0eXAiOiJKV1QiLCJhbGc..." \
  http://localhost:8000/auth/me
```

### Output

```json
{
  "id": "user-123",
  "email": "user@example.com",
  "full_name": "Test User",
  "is_student": true,
  "school": "berkeley",
  "location": "Oakland",
  "military_status": null,
  "wallet_initialized": true,
  "crypto_wallet_address": "0x742d35Cc6634C0532925a3b844Bc2e7a1b8Bef6A",
  "wallet_type": "non_custodial",
  "application_pending": false,
  "unit_application_form_ready": false
}
```

---

## Error Handling

### User Already Has Wallet

```bash
curl -X POST http://localhost:8000/wallet/create \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"wallet_type": "fawn_custodial"}'
```

**Response (400 Bad Request):**
```json
{
  "detail": "User user-123 already has a wallet: 0x742d35Cc6634C0532925a3b844Bc2e7a1b8Bef6A"
}
```

### Invalid Wallet Type

```bash
curl -X POST http://localhost:8000/wallet/create \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"wallet_type": "invalid"}'
```

**Response (422 Unprocessable Entity):**
```json
{
  "detail": [
    {
      "type": "string_pattern",
      "loc": ["body", "wallet_type"],
      "msg": "string should match pattern '^(non_custodial|fawn_custodial)$'",
      "input": "invalid"
    }
  ]
}
```

### Missing Encryption Key (Environment Not Set)

```bash
# If FAWN_ENCRYPTION_KEY not in environment
curl -X POST http://localhost:8000/wallet/create \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"wallet_type": "fawn_custodial"}'
```

**Response (500 Internal Server Error):**
```json
{
  "detail": "Wallet error: FAWN_ENCRYPTION_KEY environment variable not set"
}
```

---

## Database Storage Example

### crypto_wallets table

```sql
INSERT INTO crypto_wallets (
  id,
  user_id,
  wallet_address,
  wallet_type,
  chain,
  usdc_balance_cents,
  encrypted_private_key,
  created_at
) VALUES (
  'wallet-abc123',
  'user-123',
  '0x742d35Cc6634C0532925a3b844Bc2e7a1b8Bef6A',
  'fawn_custodial',
  'polygon',
  0,
  E'\\x8004e5b7...',  -- Fernet-encrypted bytes
  2026-07-11T12:34:56Z
);
```

### users table (updated)

```sql
UPDATE users SET
  crypto_wallet_address = '0x742d35Cc6634C0532925a3b844Bc2e7a1b8Bef6A',
  wallet_type = 'fawn_custodial',
  usdc_balance_cents = 0,
  wallet_initialized = true
WHERE id = 'user-123';
```

### user_audit_log table (7-year retention)

```sql
INSERT INTO user_audit_log (
  user_id,
  action,
  details,
  retention_expires_at,
  created_at
) VALUES (
  'user-123',
  'created_wallet',
  '{"wallet_type":"fawn_custodial","chain":"polygon","wallet_address":"0x742d35Cc6634C0532925a3b844Bc2e7a1b8Bef6A"}',
  2033-07-11T12:34:56Z,  -- 7 years from now
  2026-07-11T12:34:56Z
);
```

---

## Testing Checklist

```bash
# Setup
pip install -r requirements.txt
export FAWN_ENCRYPTION_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
export USDC_CHAIN=polygon
uvicorn main:app --reload

# Test 1: Register user
TOKEN=$(curl -X POST http://localhost:8000/auth/register \
  -d '{"email":"test@fawn.dev","password":"Test123!","full_name":"Test"}' \
  | jq -r '.access_token')
echo "Token: $TOKEN"

# Test 2: Create non_custodial wallet
curl -X POST http://localhost:8000/wallet/create \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"wallet_type":"non_custodial"}' \
  | jq '.seed_phrase' | head -1
# Should output: 12 words

# Test 3: Check persistence in /auth/me
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/auth/me \
  | jq '.wallet_initialized, .crypto_wallet_address'
# Should output: true, "0x..."

# Test 4: Create second wallet (should fail)
curl -X POST http://localhost:8000/wallet/create \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"wallet_type":"fawn_custodial"}' \
  | jq '.detail'
# Should output: "User user-123 already has a wallet: 0x..."
```

