#!/usr/bin/env bash
# Fetches the cs_CZ hunspell dictionary used by the OpenSearch "cs_hunspell" token filter.
# Source: LibreOffice dictionaries repo (github.com/LibreOffice/dictionaries), cs_CZ/ directory.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST_DIR="${SCRIPT_DIR}/../opensearch/hunspell/cs_CZ"
BASE_URL="https://raw.githubusercontent.com/LibreOffice/dictionaries/master/cs_CZ"

mkdir -p "${DEST_DIR}"

echo "Downloading cs_CZ.aff ..."
curl -fsSL "${BASE_URL}/cs_CZ.aff" -o "${DEST_DIR}/cs_CZ.aff"

echo "Downloading cs_CZ.dic ..."
curl -fsSL "${BASE_URL}/cs_CZ.dic" -o "${DEST_DIR}/cs_CZ.dic"

ENCODING_LINE="$(grep -m1 '^SET ' "${DEST_DIR}/cs_CZ.aff" || true)"
echo "Declared .aff encoding: ${ENCODING_LINE:-<none found>}"
echo "NOTE: files are left in this encoding as-is. Lucene's hunspell filter reads the SET line"
echo "      itself; re-encoding the files to UTF-8 without updating that line will break lookups."

cat > "${DEST_DIR}/settings.yml" <<'EOF'
ignore_case: true
EOF

echo "Done. Files written to ${DEST_DIR}"
ls -la "${DEST_DIR}"
