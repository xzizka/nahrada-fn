from __future__ import annotations

import logging
import signal
from pathlib import Path
from types import FrameType

from redis.exceptions import TimeoutError as RedisTimeoutError

from ftpoc.clients import (
    make_postgres_pool,
    make_redis_client,
    make_s3_client,
    make_s3_presign_client,
)
from ftpoc.config import Settings
from ftpoc.extraction import TikaExtractor
from ftpoc.indexing import DocumentIndexer
from ftpoc.metadata import DocumentRepository
from ftpoc.pipeline import IngestJob, IngestPipeline
from ftpoc.storage import DocumentStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

# Liveness signal for the container healthcheck: touched every loop tick
# (idle or not) since this process has no HTTP endpoint of its own.
HEARTBEAT_PATH = Path("/tmp/ftpoc-heartbeat")

_shutdown_requested = False


def _handle_shutdown(signum: int, frame: FrameType | None) -> None:
    global _shutdown_requested
    logger.info("Received signal %s, shutting down after current job", signum)
    _shutdown_requested = True


def run(settings: Settings) -> None:
    redis_client = make_redis_client(settings)
    # One pool shared by repository and indexer: both now talk to the same
    # Postgres database (see ftpoc.indexing), so there is no separate
    # OpenSearch client/resource to construct here anymore.
    pool = make_postgres_pool(settings)
    pipeline = IngestPipeline(
        store=DocumentStore(
            make_s3_client(settings), make_s3_presign_client(settings), settings.s3_bucket
        ),
        extractor=TikaExtractor(
            settings.tika_url, settings.tika_timeout_seconds, settings.tika_max_retries
        ),
        repository=DocumentRepository(pool),
        indexer=DocumentIndexer(pool),
        text_prefix=settings.s3_text_prefix,
        min_chars_per_page=settings.min_chars_per_page,
    )

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    logger.info("Worker started, consuming '%s'", settings.redis_queue_key)
    while not _shutdown_requested:
        HEARTBEAT_PATH.touch()
        try:
            item = redis_client.blpop(
                [settings.redis_queue_key], timeout=settings.redis_queue_timeout_seconds
            )
        except RedisTimeoutError:
            # redis-py surfaces an idle BLPOP timeout as this exception
            # rather than returning None - functionally identical to "no
            # job arrived," so just loop again.
            continue
        if item is None:
            continue
        _, payload = item
        try:
            job = IngestJob.from_json(payload)
        except (KeyError, ValueError) as exc:
            logger.error("Discarding malformed queue payload %r: %s", payload, exc)
            continue

        logger.info("Processing s3://%s/%s", job.s3_bucket, job.s3_key)
        try:
            pipeline.process(job.s3_bucket, job.s3_key)
        except Exception:
            logger.exception("Failed to process s3://%s/%s", job.s3_bucket, job.s3_key)

    logger.info("Worker stopped")


def main() -> None:
    run(Settings())  # type: ignore[call-arg]


if __name__ == "__main__":
    main()
