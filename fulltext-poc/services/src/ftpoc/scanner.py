from __future__ import annotations

import logging
import signal
import time
from pathlib import Path
from types import FrameType

from redis import Redis

from ftpoc.clients import (
    make_postgres_pool,
    make_redis_client,
    make_s3_client,
    make_s3_presign_client,
)
from ftpoc.config import Settings
from ftpoc.metadata import DocumentRepository
from ftpoc.pipeline import IngestJob
from ftpoc.storage import DocumentStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

HEARTBEAT_PATH = Path("/tmp/ftpoc-heartbeat")

_shutdown_requested = False


def _handle_shutdown(signum: int, frame: FrameType | None) -> None:
    global _shutdown_requested
    logger.info("Received signal %s, shutting down after current sweep", signum)
    _shutdown_requested = True


class Scanner:
    """Reconciliation sweep: catches objects that landed in the bucket
    without going through ingest-api (bulk CMIS import, `aws s3 sync`,
    manual upload). Compares S3's listing against PostgreSQL by (key, etag)
    and enqueues anything new or changed. Idempotent and cheap when nothing
    changed - a ListObjectsV2 pass plus indexed key lookups, no S3 writes."""

    def __init__(
        self,
        store: DocumentStore,
        repository: DocumentRepository,
        redis_client: Redis[str],
        redis_queue_key: str,
        raw_prefix: str,
    ) -> None:
        self._store = store
        self._repository = repository
        self._redis = redis_client
        self._redis_queue_key = redis_queue_key
        self._raw_prefix = raw_prefix

    def sweep(self) -> int:
        enqueued = 0
        for obj in self._store.list_objects(self._raw_prefix):
            existing = self._repository.get_by_key(self._store.bucket, obj.key)
            if existing is not None and existing.etag == obj.etag:
                continue
            logger.info(
                "Scanner found %s object s3://%s/%s",
                "new" if existing is None else "changed",
                self._store.bucket,
                obj.key,
            )
            self._redis.rpush(
                self._redis_queue_key, IngestJob(self._store.bucket, obj.key).to_json()
            )
            enqueued += 1
        return enqueued


def run(settings: Settings) -> None:
    scanner = Scanner(
        store=DocumentStore(
            make_s3_client(settings), make_s3_presign_client(settings), settings.s3_bucket
        ),
        repository=DocumentRepository(make_postgres_pool(settings)),
        redis_client=make_redis_client(settings),
        redis_queue_key=settings.redis_queue_key,
        raw_prefix=settings.s3_raw_prefix,
    )

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    logger.info(
        "Scanner started, sweeping prefix '%s' every %ds",
        settings.s3_raw_prefix,
        settings.scanner_interval_seconds,
    )
    while not _shutdown_requested:
        HEARTBEAT_PATH.touch()
        try:
            enqueued = scanner.sweep()
            if enqueued:
                logger.info("Scanner enqueued %d object(s)", enqueued)
        except Exception:
            logger.exception("Scanner sweep failed")

        for _ in range(settings.scanner_interval_seconds):
            if _shutdown_requested:
                break
            HEARTBEAT_PATH.touch()
            time.sleep(1)

    logger.info("Scanner stopped")


def main() -> None:
    run(Settings())  # type: ignore[call-arg]


if __name__ == "__main__":
    main()
