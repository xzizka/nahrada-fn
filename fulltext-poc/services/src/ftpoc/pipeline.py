from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ftpoc.extraction import TikaExtractor, TikaUnsupportedDocumentError
from ftpoc.indexing import DocumentIndexer, IndexableDocument
from ftpoc.metadata import DocumentRepository
from ftpoc.storage import DocumentStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IngestJob:
    """The queue payload. Deliberately just a location, not a hash or status -
    every trigger (API upload, scanner, bucket-notification webhook) ends up
    here, and the pipeline always re-derives the hash from the object itself
    rather than trusting whatever the producer claims."""

    s3_bucket: str
    s3_key: str

    def to_json(self) -> str:
        return json.dumps({"bucket": self.s3_bucket, "key": self.s3_key})

    @staticmethod
    def from_json(payload: str) -> IngestJob:
        data = json.loads(payload)
        return IngestJob(s3_bucket=data["bucket"], s3_key=data["key"])


class IngestPipeline:
    """Orchestrates a single object through hash -> extract -> index.

    Receives all four collaborators through the constructor; it never
    instantiates a client itself.
    """

    def __init__(
        self,
        store: DocumentStore,
        extractor: TikaExtractor,
        repository: DocumentRepository,
        indexer: DocumentIndexer,
        text_prefix: str,
        min_chars_per_page: int,
    ) -> None:
        self._store = store
        self._extractor = extractor
        self._repository = repository
        self._indexer = indexer
        self._text_prefix = text_prefix
        self._min_chars_per_page = min_chars_per_page

    def process(self, s3_bucket: str, s3_key: str) -> None:
        content = self._store.get_object(s3_key)
        sha256 = hashlib.sha256(content).hexdigest()
        info = self._store.head_object(s3_key)

        # Prefer whatever filename is already on record (set by ingest-api
        # from the actual upload) over the S3 key basename - ingest-api
        # names raw objects by content hash, so deriving a filename from the
        # key would silently clobber the real one with the hash on every
        # worker run. The key-basename fallback is only meaningful for
        # objects this pipeline is seeing for the first time (scanner/event
        # discoveries), where it's the only name information available.
        existing = self._repository.get_by_sha256(sha256)
        filename = existing.filename if existing is not None else s3_key.rsplit("/", 1)[-1]

        self._repository.register_seen(
            sha256=sha256,
            s3_bucket=s3_bucket,
            s3_key=s3_key,
            filename=filename,
            content_type=info.content_type,
            size_bytes=info.content_length,
            etag=info.etag,
        )

        # Once the row is registered, any failure below must be recorded on
        # it - otherwise a document can get stuck at 'pending'/'extracted'
        # forever, invisible to both the scanner (etag hasn't changed) and
        # to anyone reading the status column.
        try:
            extraction = self._extract(s3_key, sha256, content)
        except TikaUnsupportedDocumentError as exc:
            self._repository.mark_status(sha256, "error", str(exc))
            logger.error("Tika rejected %s (%s): %s", s3_key, sha256, exc)
            return
        except Exception as exc:
            self._repository.mark_status(sha256, "error", str(exc))
            raise

        if extraction is None:
            return  # needs_ocr, already recorded

        text, tika_version, extracted_at = extraction
        self._indexer.index_document(
            IndexableDocument(
                sha256=sha256,
                s3_bucket=s3_bucket,
                s3_key=s3_key,
                filename=filename,
                content_type=info.content_type,
                size_bytes=info.content_length,
                content=text,
                tika_version=tika_version,
                extracted_at=extracted_at,
                indexed_at=datetime.now(UTC),
            )
        )
        self._repository.mark_indexed(sha256, datetime.now(UTC))

    def _extract(self, s3_key: str, sha256: str, content: bytes) -> tuple[str, str, datetime] | None:
        """Returns (text, tika_version, extracted_at), or None if the
        document needs OCR (status already recorded in that case)."""
        if self._store.sidecar_exists(sha256, self._text_prefix):
            logger.info("Sidecar cache hit for %s, skipping Tika", sha256)
            # The needs_ocr-vs-indexable call was already made the first time
            # this content was extracted (see the else branch below); trust
            # that recorded status rather than re-deriving it, since a cache
            # hit by definition has no fresh Tika metadata to derive it from.
            record = self._repository.get_by_sha256(sha256)
            if record is not None and record.status == "needs_ocr":
                logger.info("%s (%s) still needs_ocr, not re-indexing", s3_key, sha256)
                return None
            text = self._store.get_text_sidecar(sha256, self._text_prefix)
            sidecar_info = self._store.get_sidecar_info(sha256, self._text_prefix)
            tika_version = sidecar_info.metadata.get("tika-version", "unknown")
            extracted_at = _parse_iso8601(sidecar_info.metadata.get("extracted-at"))
            # Keeps text_s3_key/tika_version/extracted_at populated even when
            # this exact (sha256) row is seeing its first cache hit (e.g. its
            # own earlier row was lost/recreated) - reindex_from_sidecars()
            # depends on all three being set for every 'indexed' row.
            self._repository.mark_extracted(sha256, self._sidecar_key(sha256), tika_version, extracted_at)
            return text, tika_version, extracted_at

        text = self._extractor.extract_text(content)
        metadata = self._extractor.extract_metadata(content)
        tika_version = self._extractor.get_version()
        extracted_at = datetime.now(UTC)
        self._store.put_text_sidecar(sha256, text, tika_version, extracted_at, self._text_prefix)
        self._repository.mark_extracted(sha256, self._sidecar_key(sha256), tika_version, extracted_at)

        chars_per_page = _chars_per_page(metadata, text)
        if chars_per_page < self._min_chars_per_page:
            self._repository.mark_status(sha256, "needs_ocr")
            logger.warning(
                "%s (%s) has too little extractable text (%.0f chars/page), marking needs_ocr",
                s3_key,
                sha256,
                chars_per_page,
            )
            return None

        return text, tika_version, extracted_at

    def reindex_from_sidecars(self) -> int:
        """Rebuilds the index purely from S3 sidecars + PostgreSQL metadata.

        Never calls Tika - this is the proof that the index is disposable.
        """
        self._indexer.recreate_index()
        count = 0
        for record in self._repository.list_indexed():
            if record.text_s3_key is None or record.tika_version is None or record.extracted_at is None:
                continue
            text = self._store.get_object(record.text_s3_key)
            self._indexer.index_document(
                IndexableDocument(
                    sha256=record.sha256,
                    s3_bucket=record.s3_bucket,
                    s3_key=record.s3_key,
                    filename=record.filename,
                    content_type=record.content_type,
                    size_bytes=record.size_bytes,
                    content=text.decode("utf-8"),
                    tika_version=record.tika_version,
                    extracted_at=record.extracted_at,
                    indexed_at=datetime.now(UTC),
                )
            )
            self._repository.mark_indexed(record.sha256, datetime.now(UTC))
            count += 1
        self._indexer.refresh()
        return count

    def _sidecar_key(self, sha256: str) -> str:
        return f"{self._text_prefix}{sha256}.txt"


def _parse_iso8601(value: str | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    return datetime.fromisoformat(value)


def _chars_per_page(metadata: dict[str, Any], text: str) -> float:
    """Prefers Tika's own pdf:charsPerPage (computed from the real PDF page
    tree) over guessing - PDF content is compressed binary, so scanning it
    for form-feed bytes to estimate a page count is not meaningful."""
    raw = metadata.get("pdf:charsPerPage")
    if isinstance(raw, list):
        raw = raw[0] if raw else None
    if raw is not None:
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass

    npages_raw = metadata.get("xmpTPg:NPages", "1")
    if isinstance(npages_raw, list):
        npages_raw = npages_raw[0] if npages_raw else "1"
    try:
        npages = max(1, int(npages_raw))
    except (TypeError, ValueError):
        npages = 1
    return len(text.strip()) / npages
