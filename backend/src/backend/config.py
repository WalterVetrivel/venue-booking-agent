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


@lru_cache
def get_settings() -> Settings:
    return Settings()
