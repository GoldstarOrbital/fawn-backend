"""
Username service for FAWN user profiles.

Handles:
- Username generation for new/existing users
- Username validation and uniqueness checking
- Username updates with duplicate prevention
"""
import re
from sqlalchemy.orm import Session
from models import User
from typing import Optional

# Username rules
MIN_LENGTH = 3
MAX_LENGTH = 30
VALID_PATTERN = re.compile(r'^[a-z0-9_]{3,30}$')
RESERVED_USERNAMES = {
    'admin', 'root', 'system', 'api', 'bot', 'founder', 'support', 'fawn',
    'payments', 'trading', 'wallet', 'settings', 'dashboard', 'notifications'
}


def is_valid_username(username: str) -> bool:
    """Check if username follows format rules."""
    if not username or not isinstance(username, str):
        return False
    username_lower = username.lower()
    if username_lower in RESERVED_USERNAMES:
        return False
    return bool(VALID_PATTERN.match(username_lower))


def generate_username(db: Session, base_name: str, max_attempts: int = 100) -> Optional[str]:
    """
    Generate a unique username based on name.

    Strategy:
    1. Try firstname_lastname (lowercase)
    2. Try firstname_l (firstname + last initial)
    3. Try firstname (if unique)
    4. Try firstname_1, firstname_2, etc. with counter
    """
    if not base_name:
        return None

    base_name_clean = base_name.lower().strip()
    # Extract first and last name
    parts = base_name_clean.split()
    if not parts:
        return None

    first = parts[0][:20]  # Cap first name
    last = parts[-1][:1] if len(parts) > 1 else ''

    candidates = []

    # Try firstname_lastname
    if len(parts) > 1:
        full = f"{first}_{parts[-1][:15]}".replace(' ', '_')
        if is_valid_username(full):
            candidates.append(full)

        # Try firstname_l
        if last:
            short = f"{first}_{last}".replace(' ', '_')
            if is_valid_username(short):
                candidates.append(short)

    # Try just firstname
    if is_valid_username(first):
        candidates.append(first)

    # Try each candidate
    for username in candidates:
        if not db.query(User).filter(User.username.ilike(username)).first():
            return username

    # Try with counter: firstname_1, firstname_2, ...
    for i in range(1, max_attempts):
        counter_name = f"{first}_{i}"
        if is_valid_username(counter_name):
            if not db.query(User).filter(User.username.ilike(counter_name)).first():
                return counter_name

    return None


def assign_username_to_user(db: Session, user: User, desired_username: Optional[str] = None) -> bool:
    """
    Assign a username to a user.

    If desired_username provided and available, use it.
    Otherwise, generate one from their full name.

    Returns True if assigned, False if failed.
    """
    if user.username:
        return True  # Already has one

    # Try desired username first
    if desired_username:
        desired_lower = desired_username.lower()
        if is_valid_username(desired_lower):
            if not db.query(User).filter(User.username.ilike(desired_lower)).first():
                user.username = desired_lower
                db.commit()
                return True

    # Generate one
    generated = generate_username(db, user.full_name)
    if generated:
        user.username = generated
        db.commit()
        return True

    return False


def update_username(db: Session, user: User, new_username: str) -> tuple[bool, str]:
    """
    Update a user's username.

    Returns (success, message)
    """
    new_username_clean = new_username.lower().strip()

    # Validate format
    if not is_valid_username(new_username_clean):
        return False, "Username must be 3-30 characters (a-z, 0-9, _)"

    # Check uniqueness (case-insensitive)
    existing = db.query(User).filter(
        User.username.ilike(new_username_clean),
        User.id != user.id  # Exclude current user
    ).first()

    if existing:
        return False, "Username already taken"

    user.username = new_username_clean
    db.commit()
    return True, "Username updated"
