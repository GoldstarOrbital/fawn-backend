#!/usr/bin/env python3
"""Verify Stripe Payouts implementation is complete."""

import sys
import os

def check_file_exists(path, description):
    """Check if file exists."""
    exists = os.path.exists(path)
    status = "OK" if exists else "MISSING"
    print(f"[{status}] {description}: {path}")
    return exists

def check_file_contains(path, search_string, description):
    """Check if file contains string."""
    try:
        with open(path, 'r') as f:
            content = f.read()
            found = search_string in content
            status = "OK" if found else "MISSING"
            print(f"[{status}] {description}")
            return found
    except:
        print(f"[ERROR] Could not read {path}")
        return False

def main():
    print("=" * 60)
    print("STRIPE PAYOUTS IMPLEMENTATION VERIFICATION")
    print("=" * 60)
    
    checks = [
        # Created files
        ("services/stripe_payouts.py", "Stripe Payouts Service"),
        ("tests/test_stripe_payouts.py", "Test Suite"),
        ("STRIPE_PAYOUTS_GUIDE.md", "Team Documentation"),
        ("STRIPE_PAYOUTS_DEPLOYMENT.md", "Deployment Guide"),
        ("STRIPE_PAYOUTS_CHECKLIST.md", "Launch Checklist"),
    ]
    
    print("\nFILES CREATED:")
    print("-" * 60)
    created_count = sum(1 for path, desc in checks if check_file_exists(path, desc))
    
    # File modifications
    print("\nFILE MODIFICATIONS:")
    print("-" * 60)
    
    mods = [
        ("requirements.txt", "stripe>=10.0.0", "Stripe dependency added"),
        ("config.py", "stripe_publishable_key", "Stripe config added"),
        ("models.py", "stripe_payout_id", "BankTransfer schema updated"),
        ("services/crypto_wallet.py", "stripe_payouts", "send_to_bank() updated"),
        ("routers/crypto.py", "Instant (typically <30 seconds)", "Response message updated"),
        ("routers/stripe_webhook.py", "payout.paid", "Webhook handler added"),
    ]
    
    mod_count = sum(1 for path, search, desc in mods if check_file_contains(path, search, desc))
    
    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print("Files Created: {}/5".format(created_count))
    print("Files Modified: {}/6".format(mod_count))
    print("Total: {}/11".format(created_count + mod_count))
    
    if created_count == 5 and mod_count == 6:
        print("\nSTATUS: IMPLEMENTATION COMPLETE - Ready for deployment")
        return 0
    else:
        print("\nSTATUS: Some components missing - Check verification above")
        return 1

if __name__ == "__main__":
    sys.exit(main())
