from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.config import get_settings


def make_engine(url: str | None = None):
    return create_engine(url or get_settings().database_url, pool_pre_ping=True)


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
