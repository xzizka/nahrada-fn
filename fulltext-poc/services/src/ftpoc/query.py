from __future__ import annotations

from dataclasses import dataclass

# ts_headline has no separate array-of-fragments output like OpenSearch's
# highlight response - MaxFragments joins them into one string on
# FragmentDelimiter instead, so search_api.py splits on this to rebuild a
# list. Not user input and contains no SQL metacharacters, so it's baked
# directly into the query text rather than bound as a parameter.
HEADLINE_FRAGMENT_DELIMITER = " ||| "

# pg_trgm's own default for its `%` operator (pg_trgm.similarity_threshold) -
# reused here via the similarity() function directly instead, since the `%`
# operator's literal `%` character would collide with psycopg's `%(name)s`
# placeholder syntax in this same query string.
_TRGM_THRESHOLD = 0.3

# ftpoc_search_tsquery() and search_tsv are defined in postgres/002-fts.sql -
# the query and the indexed column must stay analyzed the same way (Postgres
# has no equivalent of OpenSearch's multi_match "search across N differently
# analyzed fields with one query" - here that's a single generated tsvector
# column plus a matching tsquery, both built from the same three
# representations: lemmatized, verbatim, diacritics-folded).
#
# `similarity(d.filename, %(q)s)` adds fuzzy filename matching (pg_trgm,
# postgres/003-trgm.sql) alongside the tsquery match - a document whose
# filename is a close (but not exact) match for the query text is included
# even if its content doesn't match at all, with GREATEST() picking whichever
# signal scored higher.
_SEARCH_SQL = f"""
WITH q AS (SELECT ftpoc_search_tsquery(%(q)s) AS tsq)
SELECT d.filename, d.s3_key,
       GREATEST(ts_rank_cd(d.search_tsv, q.tsq), similarity(d.filename, %(q)s)) AS score,
       ts_headline(
           'simple', coalesce(d.content, ''), q.tsq,
           'StartSel=<em>, StopSel=</em>, MaxFragments=3, MinWords=5, MaxWords=35, FragmentDelimiter={HEADLINE_FRAGMENT_DELIMITER}'
       ) AS headline,
       count(*) OVER () AS total_count
FROM document d, q
WHERE d.search_tsv @@ q.tsq OR similarity(d.filename, %(q)s) > {_TRGM_THRESHOLD}
ORDER BY score DESC
LIMIT %(size)s OFFSET %(from)s
"""

# Query-term spell correction: when the strict/lemma/folded tsquery match
# above finds nothing, search_api.py calls this per word to look for a
# close corpus word to substitute and retry with - e.g. "zaruca" (typo for
# "zaruka") isn't a lexeme anywhere in search_tsv, so the tsquery match
# alone would find nothing despite the corpus containing a near-identical
# word. ts_stat() computes the distinct-lexeme vocabulary straight from
# search_tsv (lemma + verbatim + folded forms all mixed together) with no
# separate word list to maintain - deliberately a live full scan, not an
# indexed lookup, acceptable at the corpus sizes this project targets (see
# postgres/003-trgm.sql) and only ever run as a zero-results fallback, not
# on every query.
SUGGEST_WORD_SQL = f"""
SELECT word
FROM ts_stat($$SELECT search_tsv FROM document$$)
WHERE similarity(word, %(term)s) > {_TRGM_THRESHOLD}
ORDER BY similarity(word, %(term)s) DESC
LIMIT 1
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

    def build_suggestion(self, term: str) -> SearchQuery:
        return SearchQuery(sql=SUGGEST_WORD_SQL, params={"term": term})
