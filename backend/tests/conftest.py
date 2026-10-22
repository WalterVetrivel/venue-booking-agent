import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session

from backend.db import make_engine

BACKEND_DIR = Path(__file__).resolve().parents[1]
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://venue:venue_dev_pw@localhost:5432/venue_test"
)


@pytest.fixture(scope="session")
def engine():
    """Migrate a throwaway database from scratch, so the tests also check the migrations."""
    cfg = Config(BACKEND_DIR / "alembic.ini")
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    eng = make_engine(TEST_DATABASE_URL)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine):
    """A session whose work is rolled back after each test."""
    with engine.connect() as conn:
        trans = conn.begin()
        session = Session(bind=conn, join_transaction_mode="create_savepoint")
        yield session
        session.close()
        trans.rollback()
