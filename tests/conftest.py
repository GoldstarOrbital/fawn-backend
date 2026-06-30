"""Shared pytest fixtures for the FAWN backend test suite.

Forces a throwaway SQLite file DB and a known ADMIN_API_KEY before any
app module is imported, so tests never touch the real Railway Postgres DB
or the real admin key from .env.
"""
import os
import tempfile

import pytest

# Must happen before `config`/`database`/`main` are imported anywhere,
# since `settings = Settings()` is evaluated at import time.
_TMP_DB_FD, _TMP_DB_PATH = tempfile.mkstemp(suffix=".db")
os.close(_TMP_DB_FD)

os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB_PATH}"
os.environ["JWT_SECRET"] = "test_jwt_secret_for_pytest_only_not_real_1234567890"
os.environ["ADMIN_API_KEY"] = "test-admin-key-12345"
os.environ["UNIT_API_TOKEN"] = "UNIT_TOKEN_NOT_SET"  # keep Unit calls disabled in tests

from fastapi.testclient import TestClient  # noqa: E402

from database import Base, engine  # noqa: E402
import main  # noqa: E402  (imports + registers all routers, runs _init_db_schema)
from routers import auth, waitlist  # noqa: E402

main.app.state.limiter.enabled = False
auth.limiter.enabled = False
waitlist.limiter.enabled = False


@pytest.fixture(scope="session", autouse=True)
def _setup_db():
    Base.metadata.create_all(bind=engine)
    yield
    try:
        os.remove(_TMP_DB_PATH)
    except OSError:
        pass


@pytest.fixture()
def client():
    return TestClient(main.app)


@pytest.fixture()
def admin_key():
    return os.environ["ADMIN_API_KEY"]
