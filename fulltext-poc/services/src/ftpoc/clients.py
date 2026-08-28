from __future__ import annotations

from typing import TYPE_CHECKING

import boto3
from botocore.client import Config as BotoConfig
from psycopg_pool import ConnectionPool
from redis import Redis

from ftpoc.config import Settings

if TYPE_CHECKING:
    # mypy-boto3-s3 ships stubs only (a dev/type-checking dependency, not
    # installed in the runtime image) - safe to import here only because
    # `from __future__ import annotations` defers evaluation of every
    # annotation that names it.
    from mypy_boto3_s3.client import S3Client

# addressing_style="path" is required: RustFS (and most self-hosted S3
# implementations) don't own a wildcard DNS zone for virtual-host-style
# bucket subdomains.
_BOTO_CONFIG = BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"})


def make_s3_client(settings: Settings) -> S3Client:
    """S3 client for server-side use: uploads, downloads, listing."""
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name=settings.s3_region,
        config=_BOTO_CONFIG,
    )


def make_s3_presign_client(settings: Settings) -> S3Client:
    """Separate client whose endpoint is the externally-reachable one.

    Presigned URL generation is a pure local computation (no network call),
    so it's safe to point this client at a host the container itself can't
    resolve or reach.
    """
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_public_endpoint,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name=settings.s3_region,
        config=_BOTO_CONFIG,
    )


def make_redis_client(settings: Settings) -> Redis[str]:
    return Redis.from_url(settings.redis_url, decode_responses=True)


def make_postgres_pool(settings: Settings) -> ConnectionPool:
    pool = ConnectionPool(conninfo=settings.postgres_dsn, min_size=1, max_size=5, open=False)
    pool.open(wait=True, timeout=30.0)
    return pool
