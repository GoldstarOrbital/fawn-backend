"""Tests for email_templates.py — mainly guarding two real mistakes found
in review: the nurture sequence never mentioning the only live revenue
path (Founding Member), and a fabricated, unverifiable specific stat."""
from email_templates import build_email_2, build_email_3, build_email_4, build_email_5

FOUNDING_URL = "https://goldstarorbital.github.io/fawn-landing/founding.html"


def test_all_builders_return_subject_and_html():
    for build_fn in (build_email_2, build_email_3, build_email_4, build_email_5):
        subject, html = build_fn("Alex")
        assert isinstance(subject, str) and subject
        assert isinstance(html, str) and "<html>" in html
        assert "Alex" in html


def test_later_emails_link_to_founding_member_offer():
    # Email #2 (day 3) is a pure problem/empathy email — fine for it to not
    # pitch yet. From #3 onward, the sequence should give a clear path to
    # the only live revenue mechanism FAWN actually has.
    for build_fn in (build_email_3, build_email_4, build_email_5):
        _, html = build_fn("Alex")
        assert FOUNDING_URL in html


def test_no_fabricated_specific_signup_counts():
    """Regression guard: email #4 used to claim "hundreds of students have
    signed up in the last few weeks" — an unverifiable, likely-false
    specific number. Nothing in the sequence should assert a concrete
    headcount we can't actually back up."""
    for build_fn in (build_email_2, build_email_3, build_email_4, build_email_5):
        _, html = build_fn("Alex")
        lowered = html.lower()
        assert "hundreds of" not in lowered
        assert "thousands of" not in lowered
