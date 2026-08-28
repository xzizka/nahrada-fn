#!/usr/bin/env bash
# Fetches the cs_CZ hunspell dictionary used by Postgres's ispell-template
# text search dictionary (see postgres/002-fts.sql, postgres/Dockerfile).
# Source: LibreOffice dictionaries repo (github.com/LibreOffice/dictionaries), cs_CZ/ directory.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST_DIR="${SCRIPT_DIR}/../postgres/hunspell"
BASE_URL="https://raw.githubusercontent.com/LibreOffice/dictionaries/master/cs_CZ"

mkdir -p "${DEST_DIR}"

# Renamed to the .dict/.affix names Postgres's ispell dictionary template
# expects (it appends these suffixes itself to DictFile/AffFile) - same
# files, just not hunspell's own .dic/.aff extensions.
echo "Downloading cs_CZ.aff as cs_cz.affix ..."
curl -fsSL "${BASE_URL}/cs_CZ.aff" -o "${DEST_DIR}/cs_cz.affix"

echo "Downloading cs_CZ.dic as cs_cz.dict ..."
curl -fsSL "${BASE_URL}/cs_CZ.dic" -o "${DEST_DIR}/cs_cz.dict"

ENCODING_LINE="$(grep -m1 '^SET ' "${DEST_DIR}/cs_cz.affix" || true)"
echo "Declared .affix encoding: ${ENCODING_LINE:-<none found>}"
echo "NOTE: Postgres's ispell dictionary type expects these files in the database's own"
echo "      encoding (UTF-8 here, matching this file's declared SET line) - it does not"
echo "      transcode based on that line the way Lucene's hunspell filter does, so if the"
echo "      database encoding ever changes, these files must be re-encoded to match."

echo "Done. Files written to ${DEST_DIR}"
ls -la "${DEST_DIR}"
