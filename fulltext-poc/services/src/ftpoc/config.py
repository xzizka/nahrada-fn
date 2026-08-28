from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central configuration, read once from the environment.

    No component other than config.py may call os.environ / os.getenv directly.
    The word "rustfs" must never appear here — this module knows only about
    S3 endpoints and credentials, which is what keeps the storage swap a
    config change instead of a rewrite.
    """

    model_config = SettingsConfigDict(env_prefix="FTPOC_", extra="forbid")

    # S3 (works against any S3-compatible endpoint)
    s3_endpoint: str
    s3_public_endpoint: str
    s3_access_key: str
    s3_secret_key: str
    s3_region: str = "us-east-1"
    s3_bucket: str = "documents"
    s3_raw_prefix: str = "documents/"
    s3_text_prefix: str = "text/"

    # PostgreSQL (also the search index - see ftpoc.indexing/ftpoc.query)
    postgres_dsn: str

    # Redis
    redis_url: str
    redis_queue_key: str = "ftpoc:ingest"
    redis_queue_timeout_seconds: int = 5

    # Tika
    tika_url: str
    tika_timeout_seconds: float = 120.0
    tika_max_retries: int = 3

    # Pipeline behaviour
    min_chars_per_page: int = 100
    presigned_url_ttl_seconds: int = 900

    # Scanner
    scanner_interval_seconds: int = 60

    log_level: str = "INFO"
