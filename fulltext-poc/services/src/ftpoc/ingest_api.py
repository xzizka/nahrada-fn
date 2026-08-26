from __future__ import annotations

import hashlib
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from redis import Redis
from starlette.concurrency import run_in_threadpool

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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class UploadResponse(BaseModel):
    sha256: str
    status: str
    s3_bucket: str
    s3_key: str


class EventsResponse(BaseModel):
    enqueued: int


class IngestService:
    """Owns the transactional boundary for the primary trigger path: the raw
    bytes land in S3 and the PostgreSQL row is written in the same request,
    before anything touches the queue. Enqueueing is best-effort on top of
    that - if it's lost, the scanner's reconciliation sweep still finds the
    object because the PG row (or its absence) tells it something is off,
    and re-running this same upload is itself idempotent."""

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

    def ingest_upload(self, filename: str, content_type: str | None, content: bytes) -> UploadResponse:
        sha256 = hashlib.sha256(content).hexdigest()
        ext = os.path.splitext(filename)[1]
        s3_key = f"{self._raw_prefix}{sha256}{ext}"

        self._store.put_object(s3_key, content, content_type=content_type)
        status = self._repository.register_seen(
            sha256=sha256,
            s3_bucket=self._store.bucket,
            s3_key=s3_key,
            filename=filename,
            content_type=content_type,
            size_bytes=len(content),
            etag=self._store.head_object(s3_key).etag,
        )
        self._enqueue(self._store.bucket, s3_key)
        return UploadResponse(sha256=sha256, status=status, s3_bucket=self._store.bucket, s3_key=s3_key)

    def ingest_event(self, s3_bucket: str, s3_key: str) -> None:
        self._enqueue(s3_bucket, s3_key)

    def _enqueue(self, s3_bucket: str, s3_key: str) -> None:
        self._redis.rpush(self._redis_queue_key, IngestJob(s3_bucket, s3_key).to_json())


def _parse_s3_event_records(body: dict[str, Any]) -> list[tuple[str, str]]:
    records = body.get("Records", [])
    result: list[tuple[str, str]] = []
    for record in records:
        s3_info = record.get("s3", {})
        bucket = s3_info.get("bucket", {}).get("name")
        key = s3_info.get("object", {}).get("key")
        if bucket and key:
            result.append((bucket, key))
    return result


def create_app(settings: Settings) -> FastAPI:
    store = DocumentStore(
        make_s3_client(settings), make_s3_presign_client(settings), settings.s3_bucket
    )
    repository = DocumentRepository(make_postgres_pool(settings))
    redis_client = make_redis_client(settings)
    service = IngestService(
        store, repository, redis_client, settings.redis_queue_key, settings.s3_raw_prefix
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        yield

    app = FastAPI(title="ftpoc ingest-api", lifespan=lifespan)

    @app.post("/documents", response_model=UploadResponse, status_code=202)
    async def upload_document(file: UploadFile) -> UploadResponse:
        if not file.filename:
            raise HTTPException(status_code=400, detail="filename is required")
        content = await file.read()
        if not content:
            raise HTTPException(status_code=400, detail="empty file")
        result: UploadResponse = await run_in_threadpool(
            service.ingest_upload, file.filename, file.content_type, content
        )
        return result

    @app.post("/events", response_model=EventsResponse, status_code=202)
    def receive_event(body: dict[str, Any]) -> EventsResponse:
        records = _parse_s3_event_records(body)
        for bucket, key in records:
            service.ingest_event(bucket, key)
        return EventsResponse(enqueued=len(records))

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # Mounted last so it never shadows the routes above - same rationale as
    # search-api's UI mount: a thin convenience page, not part of this
    # service's actual (JSON) contract.
    static_dir = Path(__file__).parent / "static_ingest"
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="ui")

    return app


app = create_app(Settings())  # type: ignore[call-arg]
