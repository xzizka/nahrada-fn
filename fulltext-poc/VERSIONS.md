# Pinned versions and how they were established

No image below uses `:latest`. Every RustFS-specific fact here was pulled from
`docs.rustfs.com`, RustFS's GitHub repo, or Docker Hub on 2026-08-25 - not
from prior model knowledge, per the assignment's instruction that RustFS's
API is young and unreliable to recall from memory.

## Images

| Component | Image:tag | Why this tag |
|---|---|---|
| Object storage | `rustfs/rustfs:1.0.0-rc.3` | Newest published tag on Docker Hub at research time. RustFS ships no numbered stable release yet (1.0.0 is still in `-rc`), which is itself the flag: see "RustFS-specific findings" below. |
| Search index | `opensearchproject/opensearch:3.8.0` | Latest 3.x release on Docker Hub. |
| Index UI | `opensearchproject/opensearch-dashboards:3.8.0` | Matched to the OpenSearch server version - Dashboards requires a matching major.minor. |
| Text extraction | `apache/tika:3.3.1.0-full` | Latest **stable, non-preview** `-full` tag. `4.0.0-full` exists but was cut 3 days before this build and is a major version bump - **confirmed via `javap` against the real 4.0.0 jar** that it removes the fork-per-request isolation model entirely (`setNoFork`/`setTaskTimeoutMillis`/`setForkedJvmArgs`/etc are all gone from `TikaServerConfig`, replaced by an async `allowPipes` job-submission model). Using 4.0.0 with this pipeline's current synchronous `/tika`+`/meta` calls would mean a malformed document runs with no process isolation at all - a regression against the assignment's explicit isolation requirement. User confirmed staying on 3.3.1.0-full for this build rather than taking on the pipes-API rewrite. |
| Queue | `valkey/valkey:9.1.1` | User-requested swap from `redis:8.2-alpine`. Valkey is the Linux Foundation's BSD-3 Redis-protocol-compatible fork; confirmed via `docker inspect`/`docker run` that the image exposes both `valkey-cli` and a `redis-cli` shim on the same port 6379, so the `redis` Python client library and RESP protocol usage in this codebase are unaffected. Redis itself relicensed back to AGPLv3 as of 8.0, so this swap is not solving a licensing problem, just a preference. |
| Metadata store | `postgres:18.6-bookworm` | User-requested upgrade from `postgres:17.11-bookworm`. Latest 18.x point release, confirmed to exist on Docker Hub. |

Verified reachable via `docker manifest inspect` against the actual registry
before use (not just quoted from a webpage), except `postgres:17-alpine`
which timed out during a batch check and was not pursued further since
`17.11-bookworm` was already confirmed.

## RustFS-specific findings (from docs.rustfs.com + github.com/rustfs/rustfs)

**Container/env reference** (`docs.rustfs.com/en/installation/container/docker`,
`docs.rustfs.com/en/reference/environment-variables`):

- Access/secret key env vars: `RUSTFS_ACCESS_KEY` / `RUSTFS_SECRET_KEY`
  (file-based alternatives: `RUSTFS_ACCESS_KEY_FILE` / `RUSTFS_SECRET_KEY_FILE`,
  mutually exclusive with the direct value).
- S3 API bind address: `RUSTFS_ADDRESS` (default `:9000`).
- Console bind address / toggle: `RUSTFS_CONSOLE_ADDRESS` (default `:9001`),
  `RUSTFS_CONSOLE_ENABLE` (default `true`).
- Data directory: positional CLI argument to the `rustfs` binary, or
  `RUSTFS_VOLUMES` (space-separated, supports `{N...M}` ellipsis expansion for
  multi-disk/multi-node setups). Defaults to `/data` if neither is given -
  confirmed by extracting the image's actual `/entrypoint.sh`.
- Logging: `RUSTFS_OBS_LOGGER_LEVEL`, `RUSTFS_OBS_LOG_DIRECTORY`.
- Health endpoint: `curl --fail http://localhost:9000/health` (documented,
  and confirmed `curl` exists inside the image - it's Alpine-based).
- Missing/default credentials only **warn** at startup (they do not stop the
  container) - confirmed by reading the shipped entrypoint script.

**S3 compatibility matrix** (`docs.rustfs.com/en/reference/s3-compatibility`):
multipart upload, presigned URLs/SigV4, `x-amz-meta-*` user metadata,
`ListObjectsV2` with continuation tokens, bucket versioning, and Object
Lock/WORM retention are all listed as tested/available. One caveat called out
in the matrix: RustFS-encrypted objects are not portable to other S3
implementations (not relevant here - this pipeline doesn't use
server-side encryption).

**Fork mode** - verified empirically, not from docs, per the assignment's
explicit instruction:

```
$ docker run --rm --entrypoint java apache/tika:3.3.1.0-full \
    -jar /tika-server-standard-3.3.1.jar --help
usage:  tikaserver [-?] [-c <arg>] [-h <arg>] [-i <arg>] [-noFork] [-p <arg>]
 -noFork, --noFork   runs in legacy 1.x mode -- server runs in process and
                     is not safely isolated in a forked process
```

Forking is on by default; `-noFork` is the opt-out. `tika/tika-config.xml`
sets `<noFork>false</noFork>` explicitly (matching the default) precisely so
nobody "cleans it up" later without noticing what it does.

**Bucket notifications**: RustFS documents an S3-compatible
`PutBucketNotificationConfiguration`/`GetBucketNotificationConfiguration` API
(`docs.rustfs.com/en/operations/event-notifications`) with webhook/Kafka/
MQTT/MySQL/PostgreSQL/NATS/Redis/AMQP/Pulsar targets - configured via
`RUSTFS_NOTIFY_WEBHOOK_*`-style env vars for the target and ARN-based bucket
rules for the routing, applied through the standard S3 API (no SNS/Lambda
support). This is real, not vendor-webhook-only like MinIO's `mc event add` -
but wiring an actual webhook target into `docker-compose.yml` was left out of
this build (see deviations): the assignment treats it explicitly as a latency
optimization, not load-bearing, and the scanner already covers the same
gap unconditionally. `ingest-api`'s `POST /events` endpoint exists and accepts
an S3-notification-shaped body, ready to be pointed at by a configured
RustFS webhook target later.

**Hunspell filter placement** (OpenSearch, not RustFS - well-established
Lucene/Elasticsearch/OpenSearch behavior, not re-verified against young
project docs): dictionaries live under
`$OPENSEARCH_PATH_CONF/hunspell/<locale>/*.{aff,dic}` -
`/usr/share/opensearch/config/hunspell/cs_CZ/` for the official image, where
`OPENSEARCH_PATH_CONF` defaults to `/usr/share/opensearch/config`. Filter type
is `hunspell` with `locale` and `dedup` parameters. This ships in OpenSearch
core (`analysis-common`), not a separate plugin - confirmed against the
running container in Phase 2, per the phase's acceptance criterion.

## Tika server config schema

`tika/tika-config.xml`'s `<properties><server><params>` schema (host, port,
noFork, taskTimeoutMillis, taskPulseMillis, maxForkedStartupMillis,
maxRestarts, maxFiles, maxRequestSizeBytes) was pulled from the actual
Tika 3.3.1 source tree
(`raw.githubusercontent.com/apache/tika/3.3.1/tika-server/tika-server-core/src/main/resources/tika-server-config-default.xml`),
not guessed from older Tika 1.x/2.x documentation, since the server's
internal config class was refactored between major versions.
