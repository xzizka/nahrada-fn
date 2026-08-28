#!/usr/bin/env bash
# End-to-end hello-world smoke test for the fulltext-poc pipeline.
# Asserts real outcomes (search hits, log lines, HTTP codes) - not just that
# commands ran without error. Exits non-zero if any assert fails.
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PDF_PATH="${1:-$ROOT_DIR/scripts/hello-smlouva.pdf}"

set -a
# shellcheck disable=SC1091
source "$ROOT_DIR/.env"
set +a

BIND_ADDR="${BIND_ADDR:-127.0.0.1}"
INGEST_URL="http://${BIND_ADDR}:8081"
SEARCH_URL="http://${BIND_ADDR}:8080"

FAILURES=0

assert() {
    local name="$1" ok="$2" detail="$3"
    if [ "$ok" = "true" ]; then
        echo "PASS  $name"
    else
        echo "FAIL  $name -- $detail"
        FAILURES=$((FAILURES + 1))
    fi
}

pg_query() {
    docker compose -f "$ROOT_DIR/docker-compose.yml" exec -T postgres \
        psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "$1" | tr -d '[:space:]'
}

wait_for_status() {
    local sha256="$1" timeout="$2" status=""
    for _ in $(seq 1 "$timeout"); do
        status=$(pg_query "select status from document where sha256='${sha256}'")
        if [ "$status" = "indexed" ] || [ "$status" = "error" ] || [ "$status" = "needs_ocr" ]; then
            echo "$status"
            return 0
        fi
        sleep 1
    done
    echo "${status:-timeout}"
}

indexed_count() {
    pg_query "select count(*) from document where content is not null"
}

echo "== 1. bootstrap =="
make -C "$ROOT_DIR" bootstrap

echo "== 2. upload $PDF_PATH via POST /documents =="
upload_resp=$(curl -sf -X POST "${INGEST_URL}/documents" -F "file=@${PDF_PATH};type=application/pdf")
sha256=$(echo "$upload_resp" | jq -r .sha256)
echo "sha256=$sha256"

echo "== 3. waiting for status=indexed (timeout 60s) =="
status=$(wait_for_status "$sha256" 60)
assert "Document reaches status=indexed" "$([ "$status" = "indexed" ] && echo true || echo false)" "final status=$status"

echo "== Assert A: lemmatization (query 'smlouvám', absent verbatim from the text) =="
resp=$(curl -sf --get "${SEARCH_URL}/search" --data-urlencode "q=smlouvám")
total=$(echo "$resp" | jq '.total')
assert "Assert A - hunspell lemmatization" "$([ "$total" -ge 1 ] && echo true || echo false)" "total=$total"

echo "== Assert B: diacritics-free query 'zaruka' via the unaccent-folded representation =="
resp=$(curl -sf --get "${SEARCH_URL}/search" --data-urlencode "q=zaruka")
total=$(echo "$resp" | jq '.total')
assert "Assert B - asciifolded match" "$([ "$total" -ge 1 ] && echo true || echo false)" "total=$total"

echo "== Assert C: phrase query with highlight =="
resp=$(curl -sf --get "${SEARCH_URL}/search" --data-urlencode 'q="písemnou formu"')
total=$(echo "$resp" | jq '.total')
highlight_count=$(echo "$resp" | jq '[.hits[].highlights[]?] | length')
assert "Assert C - phrase match" "$([ "$total" -ge 1 ] && echo true || echo false)" "total=$total"
assert "Assert C - highlight present" "$([ "$highlight_count" -ge 1 ] && echo true || echo false)" "highlights=$highlight_count"
download_url=$(echo "$resp" | jq -r '.hits[0].download_url')

echo "== Assert D: idempotent re-upload =="
count_before=$(indexed_count)
worker_log_hits_before=$(docker compose -f "$ROOT_DIR/docker-compose.yml" logs worker 2>/dev/null | grep -c "Sidecar cache hit for ${sha256}" || true)
curl -sf -X POST "${INGEST_URL}/documents" -F "file=@${PDF_PATH};type=application/pdf" >/dev/null
sleep 5
count_after=$(indexed_count)
worker_log_hits_after=$(docker compose -f "$ROOT_DIR/docker-compose.yml" logs worker 2>/dev/null | grep -c "Sidecar cache hit for ${sha256}" || true)
assert "Assert D - document count unchanged" "$([ "$count_before" = "$count_after" ] && echo true || echo false)" "before=$count_before after=$count_after"
assert "Assert D - Tika skipped on re-upload (sidecar cache hit logged)" \
    "$([ "$worker_log_hits_after" -gt "$worker_log_hits_before" ] && echo true || echo false)" \
    "hits before=$worker_log_hits_before after=$worker_log_hits_after"

echo "== Assert E: download_url resolves from the HOST =="
# Plain GET, not `-I`/HEAD: a presigned URL is signed for one specific HTTP
# method (get_object -> GET). Requesting it with HEAD sends a different
# canonical request than what was signed, so S3 (and RustFS here) correctly
# rejects it with 403 - that's the presigned URL working as designed, not a
# bug to route around with a "real" GET being somehow less valid a check.
http_code=$(curl -s -o /dev/null -w '%{http_code}' "$download_url")
assert "Assert E - presigned download_url returns 200 from host" "$([ "$http_code" = "200" ] && echo true || echo false)" "HTTP $http_code"

echo "== Assert F: scanner discovers an object uploaded directly to S3 (bypassing ingest-api) =="
second_pdf="$ROOT_DIR/scripts/dodatek.pdf"
docker compose -f "$ROOT_DIR/docker-compose.yml" run --rm --no-deps --user root \
    -v "$ROOT_DIR/scripts:/app/scripts" ingest-api \
    python scripts/make-hello-pdf.py scripts/dodatek.pdf \
    "Dodatek číslo 1 ke smlouvě o dílo. Cena díla se navyšuje o deset procent. Termín dokončení se prodlužuje o šedesát dnů." \
    >/dev/null
second_sha256=$(sha256sum "$second_pdf" | cut -d' ' -f1)
docker compose -f "$ROOT_DIR/docker-compose.yml" run --rm --no-deps \
    -v "$ROOT_DIR/scripts:/app/scripts" ingest-api \
    python -c "
from ftpoc.clients import make_s3_client
from ftpoc.config import Settings
s = Settings()
c = make_s3_client(s)
with open('/app/scripts/dodatek.pdf', 'rb') as f:
    c.put_object(Bucket=s.s3_bucket, Key='documents/manual-import/dodatek-c-1.pdf', Body=f.read(), ContentType='application/pdf')
print('put directly to S3, bypassing ingest-api')
"
scanner_timeout=$(( ${SCANNER_INTERVAL_SECONDS:-60} + 60 ))
echo "waiting up to ${scanner_timeout}s for the scanner's next sweep..."
status=$(wait_for_status "$second_sha256" "$scanner_timeout")
assert "Assert F - scanner-discovered object reaches status=indexed" \
    "$([ "$status" = "indexed" ] && echo true || echo false)" "final status=$status"

echo
if [ "$FAILURES" -eq 0 ]; then
    echo "All asserts passed."
    exit 0
else
    echo "$FAILURES assert(s) FAILED."
    exit 1
fi
