-- Fuzzy/typo tolerance, bolted onto the FTS layer from 002-fts.sql. tsquery
-- matching (hunspell lemma / verbatim / unaccent-folded) handles inflection
-- and diacritics, but not misspellings - pg_trgm covers that gap via
-- character-trigram similarity, which degrades gracefully with edit
-- distance instead of requiring an exact lexeme.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Direct fuzzy filename matching (e.g. a slightly misremembered filename),
-- used inline in every search - see ftpoc.query's main search SQL.
CREATE INDEX IF NOT EXISTS document_filename_trgm_idx ON document USING gin (filename gin_trgm_ops);

-- Query-term spell correction (e.g. "zaruca" -> "zaruka") is done via
-- ts_stat() + similarity() directly against the corpus vocabulary at query
-- time (see ftpoc.query.SUGGEST_WORD_SQL) rather than a maintained side
-- table, so there's nothing else to create here - no separate word list to
-- keep in sync with reindex/backfill.
