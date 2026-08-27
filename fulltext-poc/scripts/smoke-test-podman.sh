#!/usr/bin/env bash
# Podman-quadlet equivalent of smoke-test.sh - identical asserts, but talks
# to the systemd-managed containers directly (podman exec/logs/run) instead
# of through podman-compose, and reads the two split env files the ftpoc
# Ansible role renders (ftpoc-secrets.env, ftpoc-app.env) instead of a single
# compose-style .env. Meant to run from the deploy directory on the target
# host (that's where those env files and this repo checkout live), e.g.:
#   BIND_ADDR=127.0.0.1 bash scripts/smoke-test-podman.sh scripts/hello-smlouva.pdf
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PDF_PATH="${1:-$ROOT_DIR/scripts/hello-smlouva.pdf}"
IMAGE="${FTPOC_IMAGE:-localhost/ftpoc-poc:local}"
NETWORK="${FTPOC_NETWORK:-ftpoc}"

set -a
# shellcheck disable=SC1091
source "$ROOT_DIR/ftpoc-secrets.env"
set +a

BIND_ADDR="${BIND_ADDR:-127.0.0.1}"
INGEST_URL="http://${BIND_ADDR}:8081"
SEARCH_URL="http://${BIND_ADDR}:8080"
OS_URL="http://${BIND_ADDR}:9200"
OS_AUTH="admin:${OPENSEARCH_INITIAL_ADMIN_PASSWORD}"

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
    podman exec postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "$1" | tr -d '[:space:]'
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

os_refresh() {
    curl -sf -u "$OS_AUTH" -X POST "${OS_URL}/documents/_refresh" >/dev/null
}

os_count() {
    curl -sf -u "$OS_AUTH" "${OS_URL}/documents/_count" | jq '.count'
}

echo "== 1. bootstrap =="
podman run --rm --network "$NETWORK" --env-file "$ROOT_DIR/ftpoc-app.env" "$IMAGE" \
    python -m ftpoc.cli bootstrap

echo "== 2. upload $PDF_PATH via POST /documents =="
upload_resp=$(curl -sf -X POST "${INGEST_URL}/documents" -F "file=@${PDF_PATH};type=application/pdf")
sha256=$(echo "$upload_resp" | jq -r .sha256)
echo "sha256=$sha256"

echo "== 3. waiting for status=indexed (timeout 60s) =="
status=$(wait_for_status "$sha256" 60)
assert "Document reaches status=indexed" "$([ "$status" = "indexed" ] && echo true || echo false)" "final status=$status"
os_refresh

echo "== Assert A: lemmatization (query 'smlouvám', absent verbatim from the text) =="
resp=$(curl -sf --get "${SEARCH_URL}/search" --data-urlencode "q=smlouvám")
total=$(echo "$resp" | jq '.total')
assert "Assert A - hunspell lemmatization" "$([ "$total" -ge 1 ] && echo true || echo false)" "total=$total"

echo "== Assert B: diacritics-free query 'zaruka' via content.folded =="
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
count_before=$(os_count)
worker_log_hits_before=$(podman logs worker 2>/dev/null | grep -c "Sidecar cache hit for ${sha256}" || true)
curl -sf -X POST "${INGEST_URL}/documents" -F "file=@${PDF_PATH};type=application/pdf" >/dev/null
sleep 10
os_refresh
count_after=$(os_count)
worker_log_hits_after=$(podman logs worker 2>/dev/null | grep -c "Sidecar cache hit for ${sha256}" || true)
assert "Assert D - document count unchanged" "$([ "$count_before" = "$count_after" ] && echo true || echo false)" "before=$count_before after=$count_after"
assert "Assert D - Tika skipped on re-upload (sidecar cache hit logged)" \
    "$([ "$worker_log_hits_after" -gt "$worker_log_hits_before" ] && echo true || echo false)" \
    "hits before=$worker_log_hits_before after=$worker_log_hits_after"

echo "== Assert E: download_url resolves from the HOST =="
# Plain GET, not `-I`/HEAD - see smoke-test.sh's comment on presigned URLs.
http_code=$(curl -s -o /dev/null -w '%{http_code}' "$download_url")
assert "Assert E - presigned download_url returns 200 from host" "$([ "$http_code" = "200" ] && echo true || echo false)" "HTTP $http_code"

echo "== Assert F: scanner discovers an object uploaded directly to S3 (bypassing ingest-api) =="
second_pdf="$ROOT_DIR/scripts/dodatek.pdf"
podman run --rm --user root -v "$ROOT_DIR/scripts:/app/scripts" "$IMAGE" \
    python scripts/make-hello-pdf.py scripts/dodatek.pdf \
    "Dodatek číslo 1 ke smlouvě o dílo. Cena díla se navyšuje o deset procent. Termín dokončení se prodlužuje o šedesát dnů." \
    >/dev/null
second_sha256=$(sha256sum "$second_pdf" | cut -d' ' -f1)
podman run --rm --network "$NETWORK" --env-file "$ROOT_DIR/ftpoc-app.env" \
    -v "$ROOT_DIR/scripts:/app/scripts" "$IMAGE" \
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
