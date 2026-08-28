-- Full-text search layer, bolted onto the metadata table from
-- 001-schema.sql. Replaces the OpenSearch index: the "index" is now just
-- this table plus a generated tsvector column, so there is no separate
-- system to keep in sync - see docs/opensearch-alternativy.md for why.

CREATE EXTENSION IF NOT EXISTS unaccent;

-- unaccent() is STABLE, not IMMUTABLE (Postgres can't prove the underlying
-- rule file never changes), which disqualifies it from a GENERATED column
-- expression or an index expression directly. This wrapper is the
-- documented workaround (see the unaccent contrib docs): we're promising
-- Postgres the rule file won't change under it, which holds for this build.
CREATE OR REPLACE FUNCTION immutable_unaccent(text) RETURNS text AS $$
    SELECT unaccent('unaccent', $1)
$$ LANGUAGE sql IMMUTABLE PARALLEL SAFE;

-- Real hunspell lemmatization (same cs_cz.dict/cs_cz.affix files as the old
-- OpenSearch build, just renamed - see postgres/Dockerfile), not Postgres's
-- built-in Snowball-based stemming. Czech isn't one of Postgres's built-in
-- text search configurations, so this copies `simple` (token categories
-- only, no dictionary) as a base rather than a language config that doesn't
-- exist.
CREATE TEXT SEARCH DICTIONARY czech_hunspell (
    TEMPLATE = ispell,
    DictFile = cs_cz,
    AffFile = cs_cz
);

CREATE TEXT SEARCH CONFIGURATION czech_hunspell (COPY = pg_catalog.simple);
ALTER TEXT SEARCH CONFIGURATION czech_hunspell
    ALTER MAPPING FOR word, asciiword, hword, hword_part, asciihword
    WITH czech_hunspell, simple;

-- Mirrors the old OpenSearch multi_match fields/boosts (content.raw^3,
-- content^2, content.folded^1, filename^2) as closely as tsvector's 4-level
-- A/B/C/D weighting allows: A = verbatim content (highest - an exact,
-- unstemmed match is the strongest signal), B = lemmatized content and
-- filename (tied, as they were both ^2), D = diacritics-free content
-- (lowest - only fires when nothing else matched). Marked IMMUTABLE for the
-- same reason as immutable_unaccent above - this build never alters
-- czech_hunspell after deployment.
CREATE OR REPLACE FUNCTION ftpoc_document_tsvector(content text, filename text) RETURNS tsvector AS $$
    SELECT
        setweight(to_tsvector('czech_hunspell', coalesce(content, '')), 'B')
        || setweight(to_tsvector('simple', coalesce(content, '')), 'A')
        || setweight(to_tsvector('simple', immutable_unaccent(coalesce(content, ''))), 'D')
        || setweight(to_tsvector('simple', coalesce(filename, '')), 'B')
$$ LANGUAGE sql IMMUTABLE PARALLEL SAFE;

-- Parses a user query the same three ways (lemmatized, verbatim, folded)
-- and ORs them together, so a query matches if any single representation
-- does - the query-side analog of the weighted column above.
CREATE OR REPLACE FUNCTION ftpoc_search_tsquery(q text) RETURNS tsquery AS $$
    SELECT plainto_tsquery('czech_hunspell', q)
        || plainto_tsquery('simple', q)
        || plainto_tsquery('simple', immutable_unaccent(q))
$$ LANGUAGE sql STABLE PARALLEL SAFE;

ALTER TABLE document ADD COLUMN content text;
ALTER TABLE document ADD COLUMN search_tsv tsvector
    GENERATED ALWAYS AS (ftpoc_document_tsvector(content, filename)) STORED;

CREATE INDEX document_search_tsv_idx ON document USING gin (search_tsv);
