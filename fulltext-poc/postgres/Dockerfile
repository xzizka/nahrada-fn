FROM postgres:18.6-bookworm

# Postgres's ispell dictionary type reads DictFile/AffFile from
# $(pg_config --sharedir)/tsearch_data, appending .dict/.affix itself - the
# files must already be there before the dictionary is first used (lazy
# load on first to_tsvector/to_tsquery call, not at CREATE TEXT SEARCH
# DICTIONARY time). Renamed from hunspell's own cs_CZ.aff/cs_CZ.dic to the
# .affix/.dict names Postgres expects; source is the same
# github.com/LibreOffice/dictionaries files scripts/fetch-hunspell.sh fetches.
COPY hunspell/cs_cz.dict hunspell/cs_cz.affix /usr/share/postgresql/18/tsearch_data/
