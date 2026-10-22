from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """App settings, read from environment variables or the repo-root .env file."""

    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str = "postgresql+psycopg://venue:venue_dev_pw@localhost:5432/venue_booking"
    admin_email: str | None = None
    data_dir: Path = REPO_ROOT / "data"
    embedding_dim: int = 384  # bge-small-en-v1.5

    timezone: str = "Asia/Kolkata"  # timetables and the campus day are in local time
    hold_hours: int = 48  # an unapproved request is released after this
    reminder_hours: int = 24  # remind the in-charge when this much of the hold is left

    session_secret: str = "dev-only-change-me"
    google_client_id: str | None = None  # OAuth web client id, for Google Sign-In
    dev_login: bool = False  # allow signing in by email alone; local testing only


@lru_cache
def get_settings() -> Settings:
    return Settings()
