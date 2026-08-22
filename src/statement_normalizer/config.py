from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://statements:statements@localhost:5432/statements"
    max_upload_bytes: int = 10 * 1024 * 1024
    sql_echo: bool = False


@lru_cache
def get_settings() -> Settings:
    """Cached so the process reads the environment once."""
    return Settings()
