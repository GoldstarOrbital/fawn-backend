"""Regression tests for routers.email_automation._send_email logging.

Bug: _send_email() returned False on a non-2xx Resend response or on any
exception, but never logged anything in either branch
(`except Exception: return False` was completely silent). Because this
function drives the entire post-waitlist drip sequence (days 0/3/7/14/21),
a failure was invisible with no error trail to investigate, and
EmailLog never gets written for the failed step, so failures recur silently
on every subsequent /internal/process-nurture run too.

Fixed to mirror routers/member.py's _send_magic_link pattern: log
status_code + response body on a non-2xx response, and log str(e) in the
except Exception branch, including which email_number/recipient failed.
"""
from unittest.mock import patch, MagicMock

import routers.email_automation as email_automation


def _settings_with_key(monkeypatch):
    monkeypatch.setattr(email_automation.settings, "resend_api_key", "test_key")


def test_returns_true_on_success(monkeypatch):
    _settings_with_key(monkeypatch)
    mock_resp = MagicMock(status_code=200, text="ok")
    with patch("routers.email_automation.httpx.post", return_value=mock_resp) as mock_post:
        result = email_automation._send_email("Subject", "<p>hi</p>", "student@example.com", email_number=3)
    assert result is True
    mock_post.assert_called_once()


def test_returns_false_and_logs_on_bad_status(monkeypatch, capsys):
    _settings_with_key(monkeypatch)
    mock_resp = MagicMock(status_code=422, text="Invalid `to` field")
    with patch("routers.email_automation.httpx.post", return_value=mock_resp):
        result = email_automation._send_email("Subject", "<p>hi</p>", "student@example.com", email_number=3)
    captured = capsys.readouterr()
    assert result is False
    assert "422" in captured.out
    assert "failed" in captured.out
    assert "student@example.com" in captured.out
    assert "3" in captured.out


def test_returns_false_and_logs_on_exception(monkeypatch, capsys):
    _settings_with_key(monkeypatch)
    with patch("routers.email_automation.httpx.post", side_effect=Exception("network down")):
        result = email_automation._send_email("Subject", "<p>hi</p>", "student@example.com", email_number=4)
    captured = capsys.readouterr()
    assert result is False
    assert "network down" in captured.out
    assert "student@example.com" in captured.out
    assert "4" in captured.out


def test_no_send_attempted_without_api_key_is_logged(monkeypatch, capsys):
    monkeypatch.setattr(email_automation.settings, "resend_api_key", "")
    with patch("routers.email_automation.httpx.post") as mock_post:
        result = email_automation._send_email("Subject", "<p>hi</p>", "student@example.com", email_number=2)
    captured = capsys.readouterr()
    assert result is False
    mock_post.assert_not_called()
    assert "student@example.com" in captured.out
