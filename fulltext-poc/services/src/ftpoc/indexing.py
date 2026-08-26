from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from opensearchpy import OpenSearch
from opensearchpy.helpers import bulk

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
    """The only component that writes to OpenSearch. Always via _bulk."""

    def __init__(self, client: OpenSearch, index_name: str) -> None:
        self._client = client
        self._index_name = index_name

    def index_document(self, document: IndexableDocument) -> None:
        self.index_documents([document])

    def index_documents(self, documents: Iterable[IndexableDocument]) -> int:
        actions = (
            {
                "_index": self._index_name,
                "_id": doc.sha256,
                "_source": {
                    "content": doc.content,
                    "filename": doc.filename,
                    "s3_bucket": doc.s3_bucket,
                    "s3_key": doc.s3_key,
                    "sha256": doc.sha256,
                    "content_type": doc.content_type,
                    "size_bytes": doc.size_bytes,
                    "tika_version": doc.tika_version,
                    "extracted_at": doc.extracted_at.isoformat(),
                    "indexed_at": doc.indexed_at.isoformat(),
                },
            }
            for doc in documents
        )
        success_count, errors = bulk(self._client, actions, raise_on_error=True)
        if errors:
            raise RuntimeError(f"Bulk indexing reported errors: {errors}")
        return int(success_count)

    def put_index_template(self, name: str, template: dict[str, Any]) -> None:
        self._client.indices.put_index_template(name=name, body=template)

    def recreate_index(self) -> None:
        if self._client.indices.exists(index=self._index_name):
            self._client.indices.delete(index=self._index_name)
        self._client.indices.create(index=self._index_name)

    def count(self) -> int:
        result: dict[str, Any] = self._client.count(index=self._index_name)
        return int(result["count"])

    def refresh(self) -> None:
        self._client.indices.refresh(index=self._index_name)
