"""Tests for POST /admin/bootstrap-alembic-stamp -- the one-time Alembic
adoption step. Verifies it correctly stamps a fresh DB and correctly
refuses to run twice (guards against accidentally re-stamping over real
migration history later)."""
import os

from sqlalchemy import text

from database import SessionLocal


def _admin_post(path):
    from main import app
    from starlette.testclient import TestClient
    client = TestClient(app)
    return client.post(path, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]})


def _drop_alembic_version_if_present():
    db = SessionLocal()
    try:
        db.execute(text("DROP TABLE IF EXISTS alembic_version"))
        db.commit()
    finally:
        db.close()


def test_stamps_a_fresh_database():
    _drop_alembic_version_if_present()
    try:
        resp = _admin_post("/admin/bootstrap-alembic-stamp")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "stamped"
        assert body["revision"]  # some real revision id, not empty

        db = SessionLocal()
        try:
            row = db.execute(text("SELECT version_num FROM alembic_version")).scalar()
            assert row == body["revision"]
        finally:
            db.close()
    finally:
        _drop_alembic_version_if_present()


def test_refuses_to_restamp_when_already_stamped():
    _drop_alembic_version_if_present()
    try:
        first = _admin_post("/admin/bootstrap-alembic-stamp")
        assert first.status_code == 200, first.text

        second = _admin_post("/admin/bootstrap-alembic-stamp")
        assert second.status_code == 409
    finally:
        _drop_alembic_version_if_present()


def test_requires_admin_key():
    from main import app
    from starlette.testclient import TestClient
    client = TestClient(app)
    resp = client.post("/admin/bootstrap-alembic-stamp")
    assert resp.status_code == 403
