"""DEPRECATED — superseded by tests/test_stripe_onboarding.py.

FAWN's BaaS provider moved from Unit to Stripe. Skipped rather than
deleted because this environment couldn't delete files in this session
(no shell access) — please delete this file.
"""
import pytest

pytest.skip("Superseded by tests/test_stripe_onboarding.py", allow_module_level=True)
