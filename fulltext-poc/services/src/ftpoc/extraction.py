from __future__ import annotations

import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class TikaUnsupportedDocumentError(Exception):
    """Tika rejected the document (4xx) - retrying will not help."""


class TikaExtractionError(Exception):
    """Tika failed after exhausting retries."""


class TikaExtractor:
    """The only component that speaks to Tika."""

    def __init__(self, base_url: str, timeout_seconds: float, max_retries: int = 3) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries

    def _put_with_retry(self, path: str, content: bytes, accept: str) -> httpx.Response:
        # Deliberately never sends a Content-Type header: it would come from
        # the uploader's browser/OS file association, which is not reliable
        # (e.g. WPS Office registers `application/wps-office.docx` for plain
        # OOXML .docx files) - confirmed empirically that Tika trusts a
        # supplied Content-Type over its own sniffing and silently returns
        # an empty body for one it doesn't recognize, with no error. Tika's
        # own content detection is the reliable part; let it do its job.
        headers = {"Accept": accept}

        last_exc: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                response = httpx.put(
                    f"{self._base_url}{path}",
                    content=content,
                    headers=headers,
                    timeout=self._timeout_seconds,
                )
            except httpx.TransportError as exc:
                last_exc = exc
                logger.warning("Tika request to %s failed (attempt %d): %s", path, attempt + 1, exc)
                time.sleep(2**attempt)
                continue

            if 400 <= response.status_code < 500:
                raise TikaUnsupportedDocumentError(
                    f"Tika rejected document with {response.status_code}: {response.text[:500]}"
                )
            if response.status_code >= 500:
                last_exc = TikaExtractionError(
                    f"Tika returned {response.status_code}: {response.text[:500]}"
                )
                logger.warning(
                    "Tika request to %s failed (attempt %d): %s", path, attempt + 1, last_exc
                )
                time.sleep(2**attempt)
                continue

            return response

        raise TikaExtractionError(
            f"Tika request to {path} failed after {self._max_retries} attempts"
        ) from last_exc

    def extract_text(self, content: bytes) -> str:
        response = self._put_with_retry("/tika", content, accept="text/plain")
        return response.text

    def extract_metadata(self, content: bytes) -> dict[str, Any]:
        response = self._put_with_retry("/meta", content, accept="application/json")
        result: dict[str, Any] = response.json()
        return result

    def get_version(self) -> str:
        response = httpx.get(f"{self._base_url}/version", timeout=self._timeout_seconds)
        response.raise_for_status()
        return response.text.strip()
