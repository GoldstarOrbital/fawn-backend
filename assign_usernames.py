#!/usr/bin/env python3
"""
Assign usernames to all existing users without one.
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from models import User
from services.username_service import assign_username_to_user

db_url = os.environ.get('DATABASE_URL', 'sqlite:///fawn.db')
print(f"[*] Connecting to: {db_url[:50]}...")

try:
    engine = create_engine(db_url)
    Session = sessionmaker(bind=engine)
    session = Session()

    # Get all users without username
    users_without_username = session.query(User).filter(User.username.is_(None)).all()
    print(f"[*] Found {len(users_without_username)} users without usernames")

    assigned_count = 0
    for user in users_without_username:
        # Special case: if this is the founder, assign @founder
        if user.email in ('alexgoldy@icloud.com', 'alexmarcusgoldsmith@gmail.com'):
            user.username = 'founder'
            session.commit()
            assigned_count += 1
            print(f"  [+] {user.email}: @founder (founder)")
        else:
            # Generate username from full name
            if assign_username_to_user(session, user):
                assigned_count += 1
                print(f"  [+] {user.email}: @{user.username}")
            else:
                print(f"  [!] {user.email}: FAILED to assign username")

    print(f"\n[+] Successfully assigned {assigned_count}/{len(users_without_username)} usernames")
    session.close()

except Exception as e:
    print(f"[!] ERROR: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
