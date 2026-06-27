"""Regression guard: no router should hardcode Resend's shared sandbox
sender (onboarding@resend.dev) as a 'from' address.

That sender can only deliver to the Resend account owner's own verified
email — sending to any real third party silently fails. Found this bug
live in waitlist.py, member.py, and stripe_webhook.py (a friend signed up
for the waitlist and never got the confirmation email). The fix in every
case is to build the sender from settings.from_email instead.
"""
import pathlib

import pytest

ROUTERS_DIR = pathlib.Path(__file__).resolve().parent.parent / "routers"


@pytest.mark.parametrize("path", sorted(ROUTERS_DIR.glob("*.py")))
def test_no_hardcoded_resend_sandbox_sender(path):
    content = path.read_text(encoding="utf-8")
    assert "onboarding@resend.dev" not in content, (
        f"{path.name} hardcodes Resend's sandbox sender, which can't deliver to real "
        f"recipients. Build the sender from settings.from_email instead."
    )
