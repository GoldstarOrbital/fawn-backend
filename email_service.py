import httpx
from config import settings


def send_waitlist_welcome(to_email: str, position: int):
    if not settings.resend_api_key:
        return

    html = f"""
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:560px;margin:0 auto;padding:32px 16px;color:#111;">
  <p style="font-size:18px;font-weight:600;margin-bottom:8px;">You're on the FAWN waitlist 🎉</p>
  <p>Hey — I'm Alex, one of the founders of FAWN. Welcome.</p>
  <p>You're <strong>#{position}</strong> on the waitlist.</p>
  <p>FAWN is a banking app built specifically for college students. No monthly fees. No minimum balance. No overdraft traps.</p>
  <p>Instead, we charge <strong>$0.01–$0.02 per transaction</strong>. That's it. Your deposits are FDIC-insured through our banking partner, Unit.</p>
  <p>Over the next few weeks I'll send you a few short emails explaining how it works and what to expect when we open access.</p>
  <p>If you have a question, just reply — it comes straight to me.</p>
  <p style="margin-top:32px;">Talk soon,<br><strong>Alex</strong><br>Co-founder, FAWN</p>
  <hr style="border:none;border-top:1px solid #eee;margin:32px 0;">
  <p style="font-size:12px;color:#888;">FAWN · <a href="https://goldstarorbital.github.io/fawn-landing" style="color:#888;">getfawn.com</a> · <a href="https://goldstarorbital.github.io/fawn-landing/privacy.html" style="color:#888;">Privacy</a></p>
</body>
</html>"""

    try:
        httpx.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {settings.resend_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": f"Alex at FAWN <{settings.from_email}>",
                "to": [to_email],
                "subject": "You're on the FAWN waitlist 🎉",
                "html": html,
            },
            timeout=10,
        )
    except Exception:
        pass  # email is best-effort, never block signup
