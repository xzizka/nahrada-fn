from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query
from fastapi.staticfiles import StaticFiles
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from ftpoc.clients import make_postgres_pool, make_s3_client, make_s3_presign_client
from ftpoc.config import Settings
from ftpoc.query import HEADLINE_FRAGMENT_DELIMITER, SearchQueryBuilder
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
    # Set only when the literal query matched nothing and a per-word
    # trigram-similarity correction (see SearchService._fuzzy_correct) found
    # something that did - absent otherwise, never present with total == 0.
    corrected_query: str | None = None


class SearchService:
    """Executes a search and shapes the response. Never builds SQL itself -
    that's SearchQueryBuilder's job - and never talks to S3 for anything but
    presigned URLs, via the shared DocumentStore."""

    def __init__(
        self,
        pool: ConnectionPool,
        query_builder: SearchQueryBuilder,
        store: DocumentStore,
        presigned_url_ttl_seconds: int,
    ) -> None:
        self._pool = pool
        self._query_builder = query_builder
        self._store = store
        self._ttl = presigned_url_ttl_seconds

    def search(self, query: str, from_: int, size: int) -> SearchResponse:
        total, hits, took_ms = self._run_query(query, from_, size)
        if total > 0:
            return SearchResponse(total=total, took_ms=took_ms, hits=hits)

        # Fuzzy fallback: the strict/lemma/folded tsquery match found
        # nothing, so this is likely a typo rather than a genuine
        # zero-result query - try substituting each word for the closest
        # corpus word (pg_trgm) and only use that if it actually finds
        # something, never as a silent replacement for a real miss.
        corrected = self._fuzzy_correct(query)
        if corrected is None:
            return SearchResponse(total=total, took_ms=took_ms, hits=hits)

        fuzzy_total, fuzzy_hits, fuzzy_took_ms = self._run_query(corrected, from_, size)
        if fuzzy_total == 0:
            return SearchResponse(total=total, took_ms=took_ms, hits=hits)

        return SearchResponse(
            total=fuzzy_total,
            took_ms=took_ms + fuzzy_took_ms,
            hits=fuzzy_hits,
            corrected_query=corrected,
        )

    def _run_query(self, query: str, from_: int, size: int) -> tuple[int, list[SearchHit], int]:
        built = self._query_builder.build(query, from_, size)

        with self._pool.connection() as conn, conn.cursor() as cur:
            started = time.monotonic()
            cur.execute(built.sql, built.params)
            rows = cur.fetchall()
            took_ms = int((time.monotonic() - started) * 1000)

        total = int(rows[0][4]) if rows else 0
        hits = [
            SearchHit(
                filename=filename,
                score=float(score) if score is not None else 0.0,
                highlights=[h for h in headline.split(HEADLINE_FRAGMENT_DELIMITER) if h],
                download_url=self._store.presigned_url(s3_key, self._ttl),
            )
            for filename, s3_key, score, headline, _total in rows
        ]
        return total, hits, took_ms

    def _fuzzy_correct(self, query: str) -> str | None:
        """Replaces each word with the closest corpus word (by trigram
        similarity) that isn't itself, if one exists. Returns None if no
        word had a correction - callers must not re-run an unchanged
        query."""
        words = query.split()
        corrected_words = []
        changed = False
        for word in words:
            suggestion = self._suggest_word(word)
            if suggestion is not None and suggestion.lower() != word.lower():
                corrected_words.append(suggestion)
                changed = True
            else:
                corrected_words.append(word)
        return " ".join(corrected_words) if changed else None

    def _suggest_word(self, word: str) -> str | None:
        built = self._query_builder.build_suggestion(word)
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(built.sql, built.params)
            row = cur.fetchone()
        return str(row[0]) if row is not None else None


def create_app(settings: Settings) -> FastAPI:
    service = SearchService(
        pool=make_postgres_pool(settings),
        query_builder=SearchQueryBuilder(),
        store=DocumentStore(
            make_s3_client(settings), make_s3_presign_client(settings), settings.s3_bucket
        ),
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
