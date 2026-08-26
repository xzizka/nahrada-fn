from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ftpoc.clients import make_opensearch_client, make_s3_client, make_s3_presign_client
from ftpoc.config import Settings
from ftpoc.query import SearchQueryBuilder
from ftpoc.storage import DocumentStore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class SearchHit(BaseModel):
    filename: str
    score: float
    highlights: list[str]
    download_url: str


class SearchResponse(BaseModel):
    total: int
    took_ms: int
    hits: list[SearchHit]


class SearchService:
    """Executes a search and shapes the response. Never builds DSL itself -
    that's SearchQueryBuilder's job - and never talks to S3 for anything but
    presigned URLs, via the shared DocumentStore."""

    def __init__(
        self,
        opensearch_client: Any,
        query_builder: SearchQueryBuilder,
        store: DocumentStore,
        index_name: str,
        presigned_url_ttl_seconds: int,
    ) -> None:
        self._client = opensearch_client
        self._query_builder = query_builder
        self._store = store
        self._index_name = index_name
        self._ttl = presigned_url_ttl_seconds

    def search(self, query: str, from_: int, size: int) -> SearchResponse:
        body = self._query_builder.build(query, from_, size)
        result = self._client.search(index=self._index_name, body=body)

        hits = []
        for hit in result["hits"]["hits"]:
            source = hit["_source"]
            highlights = hit.get("highlight", {}).get("content", [])
            hits.append(
                SearchHit(
                    filename=source["filename"],
                    score=hit["_score"] or 0.0,
                    highlights=highlights,
                    download_url=self._store.presigned_url(source["s3_key"], self._ttl),
                )
            )

        return SearchResponse(
            total=result["hits"]["total"]["value"],
            took_ms=result["took"],
            hits=hits,
        )


def create_app(settings: Settings) -> FastAPI:
    service = SearchService(
        opensearch_client=make_opensearch_client(settings),
        query_builder=SearchQueryBuilder(),
        store=DocumentStore(
            make_s3_client(settings), make_s3_presign_client(settings), settings.s3_bucket
        ),
        index_name=settings.opensearch_index,
        presigned_url_ttl_seconds=settings.presigned_url_ttl_seconds,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        yield

    app = FastAPI(title="ftpoc search-api", lifespan=lifespan)

    @app.get("/search", response_model=SearchResponse)
    def search(
        q: str = Query(..., min_length=1),
        from_: int = Query(0, alias="from", ge=0),
        size: int = Query(20, ge=1, le=100),
    ) -> SearchResponse:
        return service.search(q, from_, size)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # Mounted last so it never shadows the routes above - a minimal search
    # UI is out of the pipeline's own scope (search-api is a JSON API by
    # design), but this is a thin static page that just calls /search, not
    # a second thing this service is responsible for.
    static_dir = Path(__file__).parent / "static"
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="ui")

    return app


app = create_app(Settings())  # type: ignore[call-arg]
