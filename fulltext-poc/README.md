# fulltext-poc

Fulltext search pipeline for document content (FileNet replacement scope: a
content fulltext index, not a full ECM). RustFS (S3) for binaries, PostgreSQL
for authoritative metadata, Tika for extraction, OpenSearch for the index.
The index is derived and disposable - see `podman-compose stop tika && make
reindex` in Phase 3 below.

## Prerequisites on the host

- Podman + podman-compose (`podman-compose version`). podman-compose >=1.0
  (the version shipped by current Debian/Ubuntu/Fedora) is required for two
  things this stack relies on: honoring `depends_on: condition:
  service_healthy` (the whole startup order depends on it) and the `run
  --user ... -v ...` flag overrides used by `make hello`/`make lint`. If
  `podman-compose --version` reports something older, upgrade it rather than
  debugging startup-order or `make hello`/`make lint` failures first.
- **`vm.max_map_count >= 262144`** - OpenSearch will not start otherwise.
  ```
  sudo sysctl -w vm.max_map_count=262144
  echo 'vm.max_map_count=262144' | sudo tee /etc/sysctl.d/99-opensearch.conf
  ```
- At least ~6 GB RAM free (OpenSearch alone is given `-Xms2g -Xmx2g`; Tika,
  RustFS, Postgres, Redis need headroom on top of that).
- ~2 GB free disk for images plus space for your corpus.

## Quickstart

```
cp .env.example .env        # then edit the generated secrets
make up                     # brings up all services, waits for healthy
make bootstrap               # creates the S3 bucket + OpenSearch index template
make probe                  # Phase 1.5 - S3 API compatibility check
make hello                  # Phase 5 - generates a Czech PDF, uploads it, asserts search works
```

Then open `http://localhost:8080/` for the minimal search UI, or query
`GET /search?q=...` directly.

## Accessing this from outside the host

Every published port is bound as `${BIND_ADDR}:PORT:PORT`, not bare
`PORT:PORT` - binding only `PORT:PORT` makes Podman's rootful networking
(netavark) insert its own iptables/nftables rules ahead of the host firewall,
so a `ufw deny` on that port would not actually block it. Binding a specific
address avoids that entirely.

Three options, from safest to broadest:

| `BIND_ADDR` | Who can reach it | When to use |
|---|---|---|
| `127.0.0.1` (default) | only this host | local dev, or the API in front is on the same box |
| a specific Tailscale IP, e.g. `100.93.162.68` | only your tailnet | **recommended for real remote access** |
| `0.0.0.0` | any network this host is on, LAN included | only if you have your own firewall in front and mean it |

**This machine has Tailscale running** (`tailscale status` shows it as
`tuxedo`, IP `100.93.162.68`, MagicDNS name `tuxedo.tail777976.ts.net`). To
expose the stack to your tailnet only, without also opening it to the LAN or
the public internet, set in `.env`:

```
BIND_ADDR=100.93.162.68
PUBLIC_HOST=tuxedo.tail777976.ts.net
```

`BIND_ADDR` here is the literal Tailscale interface address, not `0.0.0.0` -
Podman binds its port-forwarding only to that interface, so the services are
simply not reachable from anywhere except the tailnet. `PUBLIC_HOST` is
separate: it's what gets baked into presigned S3 download URLs (see the
`S3_ENDPOINT` vs `S3_PUBLIC_ENDPOINT` split in Phase 4), and the MagicDNS
name is used there instead of the raw IP so it keeps working if the
Tailscale IP ever gets reassigned. Requires `tailscaled` to be up before
`make up` runs (`podman-compose up` under the hood), since the interface has
to exist before Podman can bind to it.

If you change `BIND_ADDR`/`PUBLIC_HOST` after the stack is already up, run
`make up` again (`podman-compose up -d --build`) to re-create the containers
with the new port bindings.

## Ports

| Service | Port | Published? |
|---|---|---|
| RustFS (S3 API) | 9000 | yes |
| RustFS (console) | 9001 | published, but see note below - not confirmed to actually serve a UI on `1.0.0-rc.3` |
| OpenSearch | 9200 | yes |
| OpenSearch Dashboards | 5601 | yes, `--profile dashboards` only |
| Tika | 9998 | no - internal network only |
| Queue (Valkey) | 6379 | no - internal network only |
| PostgreSQL | 5432 | yes |
| ingest-api | 8081 | yes (JSON only) |
| search-api | 8080 | yes - `GET /search` (JSON) and `GET /` (minimal search UI, see below) |

**RustFS console**: port 9001 is published and accepts connections, but a plain
`GET /` returns an S3-style XML `AccessDenied` error, not an HTML page - it
does not appear to serve a working browsable console in this build. Not
investigated further since nothing in this pipeline depends on it; use the
S3 API on port 9000 (or `make probe`) to interact with the bucket instead.

**Minimal search UI**: `search-api` serves a small static page at `/` -
a search box that calls `GET /search` and renders results with highlights
and a download link. It's a thin convenience layer, not part of the
pipeline's own architecture - `search-api`'s actual contract is still the
JSON API described above. Highlight snippets are escaped before insertion
(only the `<em>` markers OpenSearch itself inserts are re-enabled after
escaping) since that text originates from untrusted document content.

## RustFS: a flag, not a rejection

RustFS is a young project (Apache 2.0, S3-compatible, positioned as a
MinIO-community-edition replacement now that MinIO trimmed its own community
feature set). For this PoC it's a reasonable choice, and everything in this
repo talks to it through plain boto3 S3 calls - no admin API, no vendor SDK,
no bucket-notification vendor lock-in (see Phase 1.5 below and
`docker-attachment/docker-compose.yml`'s architecture notes). Swapping RustFS
for Ceph RGW, SeaweedFS, or AWS S3 should be a two-variable config change
(`FTPOC_S3_ENDPOINT`, credentials) plus re-running `make probe` to confirm
the replacement supports the same operations.

Before trusting it with a decade-scale archive, validate independently:
behavior under disk failure, how mature its erasure coding actually is in
production, and whether a real migration-out path exists (this build assumes
yes, because the pipeline only ever uses portable S3 calls - but that's an
assumption about the pipeline, not a guarantee about RustFS's on-disk
format under duress).

## Why there's no bucket-notification trigger

See `VERSIONS.md` for what RustFS's docs say notifications actually look
like. Two triggers are used instead, both S3-vendor-agnostic:

1. **`ingest-api`** (`POST /documents`) - the primary path. Upload, S3 write,
   and the PostgreSQL `status='pending'` row happen together, so the
   transaction boundary is ours, not the storage backend's.
2. **`scanner`** - a periodic `ListObjectsV2` sweep (interval:
   `SCANNER_INTERVAL_SECONDS`, default 60s) that diffs bucket contents
   against the `document` table by `(bucket, key, etag)` and enqueues
   anything new or changed. This is what catches a bulk CMIS export landing
   directly in the bucket - exactly how a FileNet migration would actually
   arrive.

`ingest-api` also exposes `POST /events`, which accepts an S3-notification-
shaped JSON body and enqueues the same job. It's wired up but nothing in
this compose file configures RustFS to actually call it - the assignment
treats this as a latency optimization on top of the scanner, not a
replacement for it, and the scanner alone is enough to pass Phase 5 Assert F.

## OpenSearch security tradeoffs (lab-appropriate, not production)

- Security plugin is **on** (basic auth via `OPENSEARCH_INITIAL_ADMIN_PASSWORD`).
- HTTP-layer TLS is **off** (`plugins.security.ssl.http.enabled=false`) so
  there's no self-signed-cert dance for a lab. In production, either turn
  TLS back on or terminate it at an ingress and never publish OpenSearch's
  port directly.
- `OPENSEARCH_INITIAL_ADMIN_PASSWORD` must satisfy OpenSearch's built-in
  complexity check (>=8 chars, upper+lower+digit+special) or the container
  exits without an obvious log message - if `opensearch` won't go healthy,
  check the password first.

## Czech analysis: hunspell, not the built-in `czech` analyzer

The built-in OpenSearch `czech` analyzer uses a **light stemmer** (suffix
rules only). Hunspell does real dictionary-based lemmatization, which is why
`make hello`'s Assert A (`smlouvám` -> matches a document that only contains
`smlouva`) is the thing that actually proves the index is usable for a legal/
contract corpus, not just that OpenSearch is up.

`opensearch/hunspell/cs_CZ/{cs_CZ.aff,cs_CZ.dic}` come from
`github.com/LibreOffice/dictionaries` via `scripts/fetch-hunspell.sh`, kept
in whatever encoding the `.aff` file's own `SET` line declares - Lucene reads
that line to decide how to interpret the dictionary, so transcoding the
files without updating it silently breaks lookups.

**RAM note**: the hunspell dictionary loads into memory on *every* data node.
On a real multi-node cluster this is a line item in the RAM budget, not a
rounding error.

**ICU folding upgrade path** (not wired into this build - kept to the
official image on purpose): `asciifolding` is used for the diacritics-free
field (`content.folded`) because it needs zero extra plugins. ICU folding
handles more cases correctly but requires a custom image:

```dockerfile
# opensearch/Dockerfile.icu (not built by this compose file)
FROM opensearchproject/opensearch:3.8.0
RUN /usr/share/opensearch/bin/opensearch-plugin install --batch analysis-icu
```

## What this PoC deliberately does not solve

- **No ACL.** `search-api` has no per-user access control. If the target
  system needs to respect FileNet's per-document permissions, every query
  must get a mandatory `terms` filter over the user's effective principals,
  enforced in the service layer - never as an optional client-supplied
  parameter, and never as post-filtering in the application (breaks
  pagination and totals).
- **No OCR.** A PDF with less extractable text than
  `min_chars_per_page` per page is marked `status='needs_ocr'` and left out
  of the index rather than indexed as an empty document. Recommended next
  step: a separate OCR worker running `ocrmypdf --language ces --skip-text`,
  writing the OCR'd PDF back to S3 as a new object, then re-enqueuing it.
  Deliberately not inside Tika - a standalone OCR step gives you control over
  timeout and cost that Tika-driven Tesseract doesn't.
- **HTTP TLS is off** on OpenSearch's client-facing port (see above).
- **RustFS and OpenSearch are both single-node**, no replication, no backup.
- **`asciifolding` instead of ICU folding** (see above).
- **No rate limiting** on `search-api`.
- **No retention policy.** If the FileNet migration needs WORM guarantees,
  RustFS's Object Lock support needs independent verification before relying
  on it - `make probe` does not exercise it, and a probe failure there would
  be an acceptable, expected gap to record, not a reason to block this PoC.

## Results

See `VERSIONS.md` for pinned versions and how they were confirmed.

### `make probe` (Phase 1.5 - S3 API compatibility)

```
STEP   CHECK                                         RESULT DETAIL
----------------------------------------------------------------------------------------------------
1      CreateBucket/HeadBucket idempotency           PASS   second CreateBucket succeeded (no-op)
2      PutObject metadata + GetObject round-trip     PASS   round-tripped unchanged
3      HeadObject (ContentLength/ContentType/ETag)   PASS   ContentLength/ContentType/ETag all correct
4      ListObjectsV2 pagination via continuation token PASS   pages=3 keys_seen=5/5
5      Multipart upload (~20MB) + SHA-256 verify     PASS   SHA-256 matched after multipart round-trip
6      Presigned GET URL (generated)                 PASS   generated for the public endpoint
7      DeleteObject + DeleteObjects (batch)          PASS   single_gone=True batch_gone=True
8      Key with diacritics and spaces                PASS   'probe/smlouvy/2026/dodatek č. 1.pdf'
6b     Presigned GET URL fetched from HOST via curl  PASS   HTTP 200
```

All 8 probe checks plus the host-side presigned-URL fetch pass. RustFS's S3
compatibility held up for everything this pipeline depends on, including the
one area young S3 implementations most often fail (multipart) and the one
area that requires cross-endpoint signing (presigned URLs signed for the
public hostname, fetched from outside Docker entirely).

### `make hello` (Phase 5 - end-to-end hello-world)

```
== 1. bootstrap ==
== 2. upload scripts/hello-smlouva.pdf via POST /documents ==
== 3. waiting for status=indexed (timeout 60s) ==
PASS  Document reaches status=indexed
== Assert A: lemmatization (query 'smlouvám', absent verbatim from the text) ==
PASS  Assert A - hunspell lemmatization
== Assert B: diacritics-free query 'zaruka' via content.folded ==
PASS  Assert B - asciifolded match
== Assert C: phrase query with highlight ==
PASS  Assert C - phrase match
PASS  Assert C - highlight present
== Assert D: idempotent re-upload ==
PASS  Assert D - document count unchanged
PASS  Assert D - Tika skipped on re-upload (sidecar cache hit logged)
== Assert E: download_url resolves from the HOST ==
PASS  Assert E - presigned download_url returns 200 from host
== Assert F: scanner discovers an object uploaded directly to S3 (bypassing ingest-api) ==
PASS  Assert F - scanner-discovered object reaches status=indexed

All asserts passed.
```

Phase 3's own acceptance test was run separately and passes independently of
the above: with `tika` stopped, `make reindex` deletes and recreates the
index from S3 sidecars + PostgreSQL metadata only, landing on the exact same
document count as before Tika was stopped.

`make lint` (ruff + mypy --strict over all 14 source files): clean, zero
findings.

## Deviations from the original spec

Everything here came from hitting real behavior empirically, not from
guessing - per the assignment's own instruction to verify rather than trust
memory, several of these are corrections to *my own* first attempt, caught
by actually running the stack rather than by re-reading documentation.

1. **RustFS has no numbered stable release.** Pinned to `1.0.0-rc.3`, the
   newest published tag - noted as a flag in the RustFS section above, not
   worked around.
2. **Tika 3.3.1 has no `maxRequestSizeBytes` setting.** The spec asks for a
   request-size limit in `tika-config.xml`. I initially added
   `<maxRequestSizeBytes>` based on a GitHub fetch that (it turned out) had
   landed on Tika's `main` branch instead of the `3.3.1` tag. `javap` against
   the actual jar in `apache/tika:3.3.1.0-full` confirmed
   `TikaServerConfig` has no such setter in this version - `tika-server`
   refused to start with a `TikaConfigException` until removed. There is no
   request-size cap configured for this build; the mitigation is
   architectural instead - Tika has no published port and is only ever
   called by `worker`, never by an untrusted client directly.
3. **A literal `--` inside an XML comment breaks Tika's config parser.**
   `tika-config.xml`'s header comment originally read `tika-server --help`;
   XML forbids `--` anywhere inside a comment body, not just at the
   delimiters, so this was a hard parse failure (`SAXParseException`), not a
   style nit.
4. **The `needs_ocr` heuristic can't scan the raw PDF for form feeds.**
   The original design compared extracted-text length against
   `min_chars_per_page * page_count`, estimating `page_count` by counting
   `\f` bytes in the PDF's raw bytes. PDF content is compressed binary, so
   this produced nonsense (a 1-page, 187-character PDF was measured at "67
   pages" and wrongly marked `needs_ocr`). Fixed by reading Tika's own
   `pdf:charsPerPage` (falling back to `xmpTPg:NPages` if absent) from the
   `/meta` endpoint, which Tika computes from the real PDF page tree.
5. **The worker was overwriting the real filename with a hash on every
   run.** `IngestPipeline.process()` derived `filename` from the S3 key
   basename unconditionally. Since ingest-api names raw objects by content
   hash (`documents/{sha256}.pdf`), every worker run replaced the real
   uploaded filename (already correctly recorded by ingest-api) with the
   ugly hashed one. Fixed by having the pipeline prefer any filename already
   on record, falling back to the key basename only for genuinely
   first-sighting documents (scanner/event-discovered objects, where the key
   basename is the only name information available at all).
6. **`redis-py`'s `BLPOP` can raise `TimeoutError` instead of returning
   `None`.** An idle blocking-pop timeout is not an error - it means no job
   arrived - but `worker` had no handling for it, so it crash-looped
   (`RestartCount` climbing) every time the queue sat idle for the timeout
   window. Now caught explicitly and treated as "loop again."
7. **A presigned URL is signed for one specific HTTP method.**
   `scripts/smoke-test.sh`'s Assert E originally checked the `download_url`
   with `curl -I` (HEAD). A URL presigned for `get_object` (GET) correctly
   fails signature validation against a HEAD request - S3, and RustFS here,
   reject it with 403 by design, not by bug. Switched the check to a plain
   GET.
8. **Host disk pressure, unrelated to this project, blocked OpenSearch
   index creation.** The host this was built on sits at ~95% disk usage from
   other, unrelated data. OpenSearch's disk-based allocator automatically
   sets a cluster-wide `create_index` block once a node crosses the high/
   flood-stage watermark - confirmed via OpenSearch's own logs
   (`DiskThresholdMonitor`), not assumed. Raised the watermarks
   (`low: 92%`, `high: 96%`, `flood_stage: 98%`) with the user's explicit
   approval rather than deleting anything on a shared machine I don't own
   the rest of. This is a host-level workaround, not a pipeline fix - a real
   deployment target needs actual free disk, not a raised alarm threshold.
9. **Tika 4.0.0 was requested mid-build as a swap-in and evaluated, then
   rejected.** `javap` against the real 4.0.0 jar confirmed it removes the
   fork-per-request isolation model entirely (`setNoFork`,
   `setTaskTimeoutMillis`, `setForkedJvmArgs`, etc. are all gone from
   `TikaServerConfig`), replacing it with an async "pipes" job-submission
   API. Adopting 4.0.0 as a like-for-like swap would have silently dropped
   the crash-isolation guarantee the assignment explicitly requires (a
   malformed document would run in-process in the main server rather than a
   disposable forked child). Given the choice between rewriting
   `TikaExtractor` against the new async API or staying on the
   already-verified `3.3.1.0-full`, the user chose to stay on 3.3.1.
10. **Redis → Valkey and PostgreSQL 17 → 18 were requested mid-build.**
    Both verified before switching: `valkey/valkey:9.1.1` ships both
    `valkey-cli` and a `redis-cli` shim on the same port, so the Python
    `redis` client and RESP usage in this codebase are unaffected. Postgres
    18's official image, however, is a genuine breaking change: it now
    refuses to start if its volume is mounted at the old
    `/var/lib/postgresql/data` convention (even against an empty volume) and
    requires mounting the parent `/var/lib/postgresql` directory instead, so
    it can manage its own major-version-specific subdirectory. Both required
    discarding the (disposable, test-only) `postgres-data` volume - PG
    metadata is authoritative in this architecture, so this is called out
    explicitly rather than glossed over, even though no real data was lost
    in this build.
11. **Forwarding the uploader's `Content-Type` to Tika silently breaks
    extraction for real documents.** Found via a real user upload: a
    698 KB `.docx` came back `status='needs_ocr'` (0 chars/page) despite
    genuinely containing 649 words of text. Root cause, isolated by calling
    Tika's `/tika` endpoint with and without the header: the browser had
    sent `Content-Type: application/wps-office.docx` (WPS Office's
    registered MIME type for a plain OOXML `.docx`), `TikaExtractor` was
    forwarding it verbatim, and Tika trusted that hint over its own content
    sniffing - returning `200 OK` with an **empty body**, no error at all.
    Without the header, Tika auto-detected the real OOXML type and
    extracted the text correctly. `TikaExtractor.extract_text`/
    `extract_metadata` no longer accept or forward a `Content-Type` at
    all - client-supplied MIME type hints are exactly the kind of untrusted
    input Tika's own detection exists to not need. (The already-`needs_ocr`
    row's sidecar - an empty file, faithfully recorded - also had to be
    deleted by hand before reprocessing, since the pipeline's own
    dedup-by-sidecar logic would otherwise have kept trusting that empty
    result as "already extracted.")
