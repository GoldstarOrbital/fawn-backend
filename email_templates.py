"""
email_templates.py

HTML email builders for the FAWN nurture sequence.
Each function takes a `name` str and returns (subject, html).
"""

_BASE_STYLE = (
    "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
    "max-width:560px;margin:0 auto;padding:32px 16px;color:#111;"
)

_SIGNATURE = (
    "<p style='margin-top:32px;'>Talk soon,<br>"
    "<strong>Alex</strong><br>"
    "Co-founder, FAWN</p>"
    "<hr style='border:none;border-top:1px solid #eee;margin:32px 0;'>"
    "<p style='font-size:12px;color:#888;'>"
    "FAWN &middot; "
    "<a href='https://goldstarorbital.github.io/fawn-landing' style='color:#888;'>getfawn.com</a>"
    "</p>"
)


def build_email_2(name: str) -> tuple[str, str]:
    """Email #2 — Day 3 — Overdraft fee explainer."""
    subject = "The $35 fee that changed how I think about banking"
    html = f"""<html><body style="{_BASE_STYLE}">
  <p>Hey {name},</p>
  <p>
    A few years ago a friend of mine overdrafted her account by $4.
    The bank charged her $35.
    Not a loan repayment fee. Not interest. Just a flat $35 penalty for being $4 short for a few hours.
  </p>
  <p>
    That's when it clicked for me: the overdraft fee isn't a safety net — it's a revenue line.
    Banks collected over <strong>$7 billion</strong> in overdraft fees last year.
    The people paying most of those fees? Young people, students, anyone living paycheck to paycheck.
  </p>
  <p>
    FAWN doesn't charge overdraft fees. Ever. If your balance would go negative, the transaction
    simply declines. No surprise charges, no shame spiral — just a declined transaction you can deal with.
  </p>
  <p>
    That's the whole idea: a bank account that doesn't make money when you're struggling.
  </p>
  <p>More soon.</p>
  {_SIGNATURE}
</body></html>"""
    return subject, html


def build_email_3(name: str) -> tuple[str, str]:
    """Email #3 — Day 7 — How FAWN makes money (transparency)."""
    subject = "How FAWN makes money (and why it matters)"
    html = f"""<html><body style="{_BASE_STYLE}">
  <p>Hey {name},</p>
  <p>
    I want to be upfront about something most banks never tell you: how they make money off you.
  </p>
  <p>
    Traditional banks profit from:
  </p>
  <ul>
    <li>Overdraft fees (~$35 a hit)</li>
    <li>Monthly maintenance fees</li>
    <li>Minimum balance penalties</li>
    <li>Keeping the interest on your deposits while paying you near-zero</li>
  </ul>
  <p>
    FAWN's model is different — and simpler:
  </p>
  <ul>
    <li>
      <strong>Interchange fees</strong> — every time you swipe your FAWN card,
      Visa pays us a small cut (typically 1–2% of the transaction, paid by the merchant, not you).
    </li>
    <li>
      <strong>Micro transaction fees</strong> — we charge <strong>$0.01–$0.02 per transaction</strong>.
      That's it.
    </li>
  </ul>
  <p>
    Here's what that means: <strong>we only make money when you use your card.</strong>
    We don't win when you overdraft. We don't win when you're broke.
    We win when you're actively using FAWN — which keeps our incentives aligned with yours.
  </p>
  <p>
    No monthly fees. No minimum balance. No gotchas.
    Your deposits are FDIC-insured through our banking partner, Stripe.
  </p>
  <p>That's the deal.</p>
  <p>
    If that resonates and you want in before everyone else, we run a small
    <strong>Founding Member</strong> program — a one-time payment that locks in
    free FAWN Premium for life and early access when we launch.
    <a href="https://goldstarorbital.github.io/fawn-landing/founding.html" style="color:#0066cc;">
      Details here →
    </a>
  </p>
  {_SIGNATURE}
</body></html>"""
    return subject, html


def build_email_4(name: str) -> tuple[str, str]:
    """Email #4 — Day 14 — Referral push with social proof."""
    subject = "You can move up the waitlist right now"
    ref_param = name.lower().replace(" ", "")
    html = f"""<html><body style="{_BASE_STYLE}">
  <p>Hey {name},</p>
  <p>
    Quick update on where things stand: the waitlist keeps growing, and your spot
    on it isn't fixed — every person you refer moves you up.
  </p>
  <p>
    The more people you bring in, the sooner you get access. And if you'd rather
    not wait at all, the
    <a href="https://goldstarorbital.github.io/fawn-landing/founding.html" style="color:#0066cc;">Founding Member program</a>
    is open right now — a one-time payment for early access plus free Premium for life.
  </p>
  <p>
    Your personal referral link:<br>
    <a href="https://goldstarorbital.github.io/fawn-landing?ref={ref_param}"
       style="color:#0066cc;word-break:break-all;">
      https://goldstarorbital.github.io/fawn-landing?ref={ref_param}
    </a>
  </p>
  <p>
    Share it in your group chat, your dorm Slack, anywhere your friends are complaining
    about bank fees. Every signup through your link counts.
  </p>
  <p>
    Beta spots are limited — we're keeping the first cohort small so we can give
    everyone a good experience. Referring is the fastest way to the front.
  </p>
  {_SIGNATURE}
</body></html>"""
    return subject, html


def build_email_5(name: str) -> tuple[str, str]:
    """Email #5 — Day 21 — Beta access teaser."""
    subject = "Beta spots are opening up — you're close"
    html = f"""<html><body style="{_BASE_STYLE}">
  <p>Hey {name},</p>
  <p>
    I wanted to give you a heads-up before we announce this broadly:
    <strong>FAWN beta is launching soon.</strong>
  </p>
  <p>
    We're starting with a small cohort — people who've been on the waitlist since early on,
    and people who've referred friends. You're in the top group.
  </p>
  <p>
    Here's what beta members get:
  </p>
  <ul>
    <li>Early access to the FAWN card and account</li>
    <li>Direct line to the founders — your feedback shapes the product</li>
    <li>Founding member status (we'll remember who showed up early)</li>
  </ul>
  <p>
    We'll send final access instructions to beta members first.
    If you haven't already, sharing your referral link is still the best way to
    move up the list — or, if you don't want to wait at all, you can claim a
    <a href="https://goldstarorbital.github.io/fawn-landing/founding.html" style="color:#0066cc;">Founding Member spot</a>
    right now and skip straight to the front.
  </p>
  <p>
    Almost there. Stay tuned.
  </p>
  {_SIGNATURE}
</body></html>"""
    return subject, html
