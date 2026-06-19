import httpx
from config import settings

OWNER_EMAIL = "alexmarcusgoldsmith@gmail.com"


def send_waitlist_welcome(to_email: str, position: int):
    """Send transactional email on waitlist signup.

    While using onboarding@resend.dev (Resend test domain), emails can only
    go to the account owner. So we notify Alex of each new signup.
    Once a verified domain is added, switch FROM_EMAIL and send to `to_email`.
    """
    if not settings.resend_api_key:
        return

    using_test_domain = "resend.dev" in settings.from_email

    if using_test_domain:
        # Notify Alex of every new signup
        subject = f"🎉 New FAWN waitlist signup #{position}"
        html = f"""
<html><body style="font-family:-apple-system,sans-serif;max-width:480px;margin:0 auto;padding:24px;color:#111;">
  <h2 style="margin:0 0 16px;">New waitlist signup #{position}</h2>
  <p><strong>Email:</strong> {to_email}</p>
  <p>They're #{position} on the FAWN waitlist.</p>
  <p style="margin-top:24px;font-size:12px;color:#888;">
    <a href="https://goldstarorbital.github.io/fawn-landing">Landing page</a> ·
    <a href="https://web-production-13d5b.up.railway.app/waitlist/count">Waitlist count API</a>
  </p>
</body></html>"""
        recipient = OWNER_EMAIL
    else:
        # Send welcome email directly to the new signup
        subject = "You're on the FAWN waitlist 🎉"
        html = f"""
<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:560px;margin:0 auto;padding:32px 16px;color:#111;">
  <p style="font-size:18px;font-weight:600;margin-bottom:8px;">You're on the FAWN waitlist 🎉</p>
  <p>Hey — I'm Alex, one of the founders of FAWN. Welcome.</p>
  <p>You're <strong>#{position}</strong> on the waitlist.</p>
  <p>FAWN is a banking app built specifically for college students. No monthly fees. No minimum balance. No overdraft traps.</p>
  <p>Instead, we charge <strong>$0.01–$0.02 per transaction</strong>. That's it. Your deposits are FDIC-insured through our banking partner, Unit.</p>
  <p>Over the next few weeks I'll send you a few short emails explaining how it works and what to expect when we open access.</p>
  <p>If you have a question, just reply — it comes straight to me.</p>
  <p style="margin-top:32px;">Talk soon,<br><strong>Alex</strong><br>Co-founder, FAWN</p>
  <hr style="border:none;border-top:1px solid #eee;margin:32px 0;">
  <p style="font-size:12px;color:#888;">FAWN · <a href="https://goldstarorbital.github.io/fawn-landing" style="color:#888;">getfawn.com</a></p>
</body></html>"""
        recipient = to_email

    try:
        httpx.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {settings.resend_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": f"Alex at FAWN <{settings.from_email}>",
                "to": [recipient],
                "subject": subject,
                "html": html,
            },
            timeout=10,
        )
    except Exception:
        pass  # best-effort, never block signup
