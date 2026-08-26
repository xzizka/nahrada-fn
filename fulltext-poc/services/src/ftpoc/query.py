from __future__ import annotations

from typing import Any


class SearchQueryBuilder:
    """Builds OpenSearch query DSL. The only place `multi_match`/`highlight`
    bodies are constructed - HTTP handlers pass query params in and get a
    ready-to-send request body out, the same way a controller hands SQL
    construction off to a repository."""

    def build(self, query: str, from_: int, size: int) -> dict[str, Any]:
        return {
            "from": from_,
            "size": size,
            "query": {
                "multi_match": {
                    "query": query,
                    "type": "best_fields",
                    "fields": [
                        "content.raw^3",
                        "content^2",
                        "content.folded^1",
                        "filename^2",
                    ],
                }
            },
            "highlight": {
                "fields": {"content": {"fragment_size": 200, "number_of_fragments": 3}}
            },
        }
