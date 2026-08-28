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
                    # Typo tolerance: AUTO scales allowed edit distance with
                    # term length (0 for 1-2 chars, 1 for 3-5, 2 for 6+) -
                    # OpenSearch's own recommended default rather than a
                    # fixed distance, which would be too lax on short terms
                    # and too strict on long ones. prefix_length=1 keeps the
                    # first character exact, which is both a common
                    # least-surprise convention and cheaper to evaluate
                    # (fewer candidate terms to edit-distance against).
                    "fuzziness": "AUTO",
                    "prefix_length": 1,
                }
            },
            "highlight": {
                "fields": {"content": {"fragment_size": 200, "number_of_fragments": 3}}
            },
        }
