from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from psycopg_pool import ConnectionPool

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IndexableDocument:
    sha256: str
    s3_bucket: str
    s3_key: str
    filename: str
    content_type: str | None
    size_bytes: int
    content: str
    tika_version: str
    extracted_at: datetime
    indexed_at: datetime


class DocumentIndexer:
    """The only component that writes searchable content. Writes `content`
    into the same `document` row `DocumentRepository` manages - the
    `search_tsv` GENERATED column (see postgres/002-fts.sql) recomputes
    itself from that on every write, so there is no separate index to keep
    in sync the way there was with OpenSearch."""

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def index_document(self, document: IndexableDocument) -> None:
        self.index_documents([document])

    def index_documents(self, documents: Iterable[IndexableDocument]) -> int:
        count = 0
        with self._pool.connection() as conn, conn.cursor() as cur:
            for doc in documents:
                cur.execute(
                    "UPDATE document SET content = %s WHERE sha256 = %s",
                    (doc.content, doc.sha256),
                )
                count += cur.rowcount
        return count

    def recreate_index(self) -> None:
        """No-op: unlike OpenSearch, there is no separate index to drop and
        recreate. `search_tsv` is a GENERATED column that recomputes itself
        from `content` on every write, so reindex_from_sidecars() rewriting
        `content` for every indexed row (see pipeline.py) is itself the
        rebuild."""

    def count(self) -> int:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM document WHERE content IS NOT NULL")
            row = cur.fetchone()
            assert row is not None
            return int(row[0])

    def refresh(self) -> None:
        """No-op: Postgres has no OpenSearch-style refresh/near-real-time
        delay - a committed UPDATE is immediately visible to the GIN index
        for the next query."""
