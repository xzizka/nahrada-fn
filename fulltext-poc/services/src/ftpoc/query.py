from __future__ import annotations

from dataclasses import dataclass

# ts_headline has no separate array-of-fragments output like OpenSearch's
# highlight response - MaxFragments joins them into one string on
# FragmentDelimiter instead, so search_api.py splits on this to rebuild a
# list. Not user input and contains no SQL metacharacters, so it's baked
# directly into the query text rather than bound as a parameter.
HEADLINE_FRAGMENT_DELIMITER = " ||| "

# ftpoc_search_tsquery() and search_tsv are defined in postgres/002-fts.sql -
# the query and the indexed column must stay analyzed the same way (Postgres
# has no equivalent of OpenSearch's multi_match "search across N differently
# analyzed fields with one query" - here that's a single generated tsvector
# column plus a matching tsquery, both built from the same three
# representations: lemmatized, verbatim, diacritics-folded).
_SEARCH_SQL = f"""
WITH q AS (SELECT ftpoc_search_tsquery(%(q)s) AS tsq)
SELECT d.filename, d.s3_key,
       ts_rank_cd(d.search_tsv, q.tsq) AS score,
       ts_headline(
           'simple', coalesce(d.content, ''), q.tsq,
           'StartSel=<em>, StopSel=</em>, MaxFragments=3, MinWords=5, MaxWords=35, FragmentDelimiter={HEADLINE_FRAGMENT_DELIMITER}'
       ) AS headline,
       count(*) OVER () AS total_count
FROM document d, q
WHERE d.search_tsv @@ q.tsq
ORDER BY score DESC
LIMIT %(size)s OFFSET %(from)s
"""


@dataclass(frozen=True)
class SearchQuery:
    sql: str
    params: dict[str, object]


class SearchQueryBuilder:
    """Builds the parameterized SQL for a search request. The only place
    the tsquery/ts_rank_cd/ts_headline call is constructed - HTTP handlers
    pass query params in and get a ready-to-execute statement out, the same
    way a controller hands SQL construction off to a repository."""

    def build(self, query: str, from_: int, size: int) -> SearchQuery:
        return SearchQuery(
            sql=_SEARCH_SQL,
            params={"q": query, "from": from_, "size": size},
        )
