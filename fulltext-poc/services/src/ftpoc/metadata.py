from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from psycopg.rows import class_row
from psycopg_pool import ConnectionPool


@dataclass(frozen=True)
class DocumentRecord:
    sha256: str
    s3_bucket: str
    s3_key: str
    text_s3_key: str | None
    filename: str
    content_type: str | None
    size_bytes: int
    etag: str | None
    tika_version: str | None
    extracted_at: datetime | None
    indexed_at: datetime | None
    seen_at: datetime
    status: str
    error: str | None


class DocumentRepository:
    """The only component that speaks to PostgreSQL. No SQL lives outside this class."""

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def register_seen(
        self,
        *,
        sha256: str,
        s3_bucket: str,
        s3_key: str,
        filename: str,
        content_type: str | None,
        size_bytes: int,
        etag: str | None,
    ) -> str:
        """Idempotent upsert keyed by content hash.

        A given (bucket, key) can, over time, come to hold different content
        (an external process overwrites the object) - that violates the
        UNIQUE(s3_bucket, s3_key) constraint if handled with a naive
        ON CONFLICT(sha256) upsert, so the stale row for that key is retired
        first. Returns the resulting row's status.
        """
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT sha256 FROM document WHERE s3_bucket = %s AND s3_key = %s",
                    (s3_bucket, s3_key),
                )
                existing = cur.fetchone()
                if existing is not None and existing[0] != sha256:
                    cur.execute("DELETE FROM document WHERE sha256 = %s", (existing[0],))

                cur.execute(
                    """
                    INSERT INTO document
                        (sha256, s3_bucket, s3_key, filename, content_type, size_bytes, etag, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending')
                    ON CONFLICT (sha256) DO UPDATE SET
                        s3_bucket = EXCLUDED.s3_bucket,
                        s3_key = EXCLUDED.s3_key,
                        filename = EXCLUDED.filename,
                        content_type = EXCLUDED.content_type,
                        size_bytes = EXCLUDED.size_bytes,
                        etag = EXCLUDED.etag,
                        seen_at = now()
                    RETURNING status
                    """,
                    (sha256, s3_bucket, s3_key, filename, content_type, size_bytes, etag),
                )
                row = cur.fetchone()
                assert row is not None
                return row[0]  # type: ignore[no-any-return]

    def get_by_key(self, s3_bucket: str, s3_key: str) -> DocumentRecord | None:
        with self._pool.connection() as conn:
            with conn.cursor(row_factory=class_row(DocumentRecord)) as cur:
                cur.execute(
                    "SELECT * FROM document WHERE s3_bucket = %s AND s3_key = %s",
                    (s3_bucket, s3_key),
                )
                return cur.fetchone()

    def get_by_sha256(self, sha256: str) -> DocumentRecord | None:
        with self._pool.connection() as conn:
            with conn.cursor(row_factory=class_row(DocumentRecord)) as cur:
                cur.execute("SELECT * FROM document WHERE sha256 = %s", (sha256,))
                return cur.fetchone()

    def mark_status(self, sha256: str, status: str, error: str | None = None) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE document SET status = %s, error = %s WHERE sha256 = %s",
                (status, error, sha256),
            )

    def mark_extracted(
        self, sha256: str, text_s3_key: str, tika_version: str, extracted_at: datetime
    ) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """
                UPDATE document
                SET text_s3_key = %s, tika_version = %s, extracted_at = %s, status = 'extracted'
                WHERE sha256 = %s
                """,
                (text_s3_key, tika_version, extracted_at, sha256),
            )

    def mark_indexed(self, sha256: str, indexed_at: datetime) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE document SET indexed_at = %s, status = 'indexed', error = NULL WHERE sha256 = %s",
                (indexed_at, sha256),
            )

    def list_by_status(self, status: str) -> list[DocumentRecord]:
        with self._pool.connection() as conn:
            with conn.cursor(row_factory=class_row(DocumentRecord)) as cur:
                cur.execute("SELECT * FROM document WHERE status = %s ORDER BY seen_at", (status,))
                return cur.fetchall()

    def list_indexed(self) -> list[DocumentRecord]:
        """Rows currently considered part of the index - the reindex source
        of truth. Deliberately scoped to status='indexed' rather than "has a
        text_s3_key", since a needs_ocr row also has a sidecar but must not
        be reindexed as if it were searchable content."""
        with self._pool.connection() as conn:
            with conn.cursor(row_factory=class_row(DocumentRecord)) as cur:
                cur.execute("SELECT * FROM document WHERE status = 'indexed' ORDER BY seen_at")
                return cur.fetchall()

    def count_by_status(self, status: str) -> int:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT count(*) FROM document WHERE status = %s", (status,)
            ).fetchone()
            assert row is not None
            return row[0]  # type: ignore[no-any-return]
