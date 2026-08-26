from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.client import Config as BotoConfig
from botocore.exceptions import ClientError

if TYPE_CHECKING:
    # Stub-only dev dependency - see clients.py for why this is safe here.
    from mypy_boto3_s3.client import S3Client

from ftpoc.clients import (
    make_opensearch_client,
    make_postgres_pool,
    make_redis_client,
    make_s3_client,
    make_s3_presign_client,
)
from ftpoc.config import Settings
from ftpoc.extraction import TikaExtractor
from ftpoc.indexing import DocumentIndexer
from ftpoc.metadata import DocumentRepository
from ftpoc.pipeline import IngestPipeline
from ftpoc.scanner import Scanner
from ftpoc.storage import DocumentStore


@dataclass(frozen=True)
class ProbeResult:
    step: str
    description: str
    passed: bool
    detail: str


def _print_probe_table(results: list[ProbeResult]) -> None:
    print()
    print(f"{'STEP':<6} {'CHECK':<45} {'RESULT':<6} DETAIL")
    print("-" * 100)
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        print(f"{r.step:<6} {r.description:<45} {status:<6} {r.detail}")
    print()


def cmd_bootstrap(settings: Settings) -> int:
    store = DocumentStore(
        make_s3_client(settings), make_s3_presign_client(settings), settings.s3_bucket
    )
    store.ensure_bucket()
    print(f"Bucket '{settings.s3_bucket}' ready")

    template_path = os.path.join(os.getcwd(), "opensearch", "index-template.json")
    with open(template_path, encoding="utf-8") as f:
        template = json.load(f)

    indexer = DocumentIndexer(make_opensearch_client(settings), settings.opensearch_index)
    indexer.put_index_template("documents", template)
    print("Index template 'documents' installed")

    opensearch_client = make_opensearch_client(settings)
    if not opensearch_client.indices.exists(index=settings.opensearch_index):
        opensearch_client.indices.create(index=settings.opensearch_index)
        print(f"Index '{settings.opensearch_index}' created")
    else:
        print(f"Index '{settings.opensearch_index}' already exists")

    return 0


def cmd_backfill(settings: Settings) -> int:
    scanner = Scanner(
        store=DocumentStore(
            make_s3_client(settings), make_s3_presign_client(settings), settings.s3_bucket
        ),
        repository=DocumentRepository(make_postgres_pool(settings)),
        redis_client=make_redis_client(settings),
        redis_queue_key=settings.redis_queue_key,
        raw_prefix=settings.s3_raw_prefix,
    )
    enqueued = scanner.sweep()
    print(f"Backfill enqueued {enqueued} object(s) missing from the index")
    return 0


def cmd_reindex(settings: Settings) -> int:
    pipeline = IngestPipeline(
        store=DocumentStore(
            make_s3_client(settings), make_s3_presign_client(settings), settings.s3_bucket
        ),
        extractor=TikaExtractor(settings.tika_url, settings.tika_timeout_seconds),
        repository=DocumentRepository(make_postgres_pool(settings)),
        indexer=DocumentIndexer(make_opensearch_client(settings), settings.opensearch_index),
        text_prefix=settings.s3_text_prefix,
        min_chars_per_page=settings.min_chars_per_page,
    )
    count = pipeline.reindex_from_sidecars()
    print(f"Reindexed {count} document(s) from S3 sidecars, without calling Tika")
    return 0


def cmd_s3_probe(settings: Settings) -> int:
    config = BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"})
    client = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name=settings.s3_region,
        config=config,
    )
    presign_client = boto3.client(
        "s3",
        endpoint_url=settings.s3_public_endpoint,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name=settings.s3_region,
        config=config,
    )

    bucket = settings.s3_bucket
    probe_prefix = "probe/"
    results: list[ProbeResult] = []

    # 1. CreateBucket, HeadBucket, repeated CreateBucket
    try:
        try:
            client.create_bucket(Bucket=bucket)
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                raise
        client.head_bucket(Bucket=bucket)
        try:
            client.create_bucket(Bucket=bucket)
            repeat_detail = "second CreateBucket succeeded (no-op)"
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                raise
            repeat_detail = f"second CreateBucket raised {code} as expected"
        results.append(ProbeResult("1", "CreateBucket/HeadBucket idempotency", True, repeat_detail))
    except Exception as exc:
        results.append(ProbeResult("1", "CreateBucket/HeadBucket idempotency", False, str(exc)))

    # 2. PutObject with x-amz-meta-* + GetObject, metadata round-trip
    try:
        key = f"{probe_prefix}metadata-roundtrip.txt"
        # S3 user metadata values must be US-ASCII (HTTP header constraint,
        # enforced client-side by boto3 for every S3 implementation, not a
        # RustFS quirk) - diacritics in *keys* are covered separately by
        # check 8 below, which is the legitimate place for that concern.
        sent_metadata = {"probe-key": "hodnota-bez-diakritiky-123", "probe-run": "ftpoc"}
        client.put_object(Bucket=bucket, Key=key, Body=b"hello", Metadata=sent_metadata)
        response = client.get_object(Bucket=bucket, Key=key)
        received_metadata = response.get("Metadata", {})
        ok = received_metadata.get("probe-key") == sent_metadata["probe-key"]
        results.append(
            ProbeResult(
                "2",
                "PutObject metadata + GetObject round-trip",
                ok,
                f"sent={sent_metadata} received={received_metadata}",
            )
        )
    except Exception as exc:
        results.append(ProbeResult("2", "PutObject metadata + GetObject round-trip", False, str(exc)))

    # 3. HeadObject
    try:
        key = f"{probe_prefix}head-check.bin"
        body = b"0123456789" * 5
        client.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/octet-stream")
        head = client.head_object(Bucket=bucket, Key=key)
        ok = (
            head["ContentLength"] == len(body)
            and head.get("ContentType") == "application/octet-stream"
            and bool(head.get("ETag"))
        )
        results.append(
            ProbeResult(
                "3",
                "HeadObject (ContentLength/ContentType/ETag)",
                ok,
                f"ContentLength={head['ContentLength']} ContentType={head.get('ContentType')} ETag={head.get('ETag')}",
            )
        )
    except Exception as exc:
        results.append(ProbeResult("3", "HeadObject (ContentLength/ContentType/ETag)", False, str(exc)))

    # 4. ListObjectsV2 with prefix + MaxKeys=2 across multiple pages
    try:
        list_prefix = f"{probe_prefix}listing/"
        for i in range(5):
            client.put_object(Bucket=bucket, Key=f"{list_prefix}item-{i}.txt", Body=b"x")
        seen_keys: set[str] = set()
        pages = 0
        token: str | None = None
        while True:
            kwargs: dict[str, str | int] = {"Bucket": bucket, "Prefix": list_prefix, "MaxKeys": 2}
            if token:
                kwargs["ContinuationToken"] = token
            page = client.list_objects_v2(**kwargs)  # type: ignore[arg-type]
            pages += 1
            seen_keys.update(o["Key"] for o in page.get("Contents", []))
            if not page.get("IsTruncated"):
                break
            token = page.get("NextContinuationToken")
            if not token:
                raise RuntimeError("IsTruncated=True but no NextContinuationToken returned")
        ok = len(seen_keys) == 5 and pages >= 3
        results.append(
            ProbeResult(
                "4",
                "ListObjectsV2 pagination via continuation token",
                ok,
                f"pages={pages} keys_seen={len(seen_keys)}/5",
            )
        )
    except Exception as exc:
        results.append(
            ProbeResult("4", "ListObjectsV2 pagination via continuation token", False, str(exc))
        )

    # 5. Multipart upload ~20MB + download + SHA-256 check
    try:
        key = f"{probe_prefix}multipart-20mb.bin"
        payload = os.urandom(20 * 1024 * 1024)
        expected_sha256 = hashlib.sha256(payload).hexdigest()
        client.upload_fileobj(
            io.BytesIO(payload),
            bucket,
            key,
            Config=TransferConfig(multipart_threshold=5 * 1024 * 1024, multipart_chunksize=5 * 1024 * 1024),
        )
        downloaded = client.get_object(Bucket=bucket, Key=key)["Body"].read()
        actual_sha256 = hashlib.sha256(downloaded).hexdigest()
        ok = actual_sha256 == expected_sha256
        results.append(
            ProbeResult(
                "5",
                "Multipart upload (~20MB) + SHA-256 verify",
                ok,
                f"expected={expected_sha256[:12]}... actual={actual_sha256[:12]}...",
            )
        )
    except Exception as exc:
        results.append(ProbeResult("5", "Multipart upload (~20MB) + SHA-256 verify", False, str(exc)))

    # 6. Presigned GET URL - generated here, verified from the HOST by `make probe`
    try:
        key = f"{probe_prefix}presigned-check.txt"
        client.put_object(Bucket=bucket, Key=key, Body=b"presigned-ok")
        url = presign_client.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=300
        )
        print(f"PRESIGNED_URL: {url}")
        results.append(
            ProbeResult(
                "6",
                "Presigned GET URL (verified from host by `make probe`)",
                True,
                "generated - see PRESIGNED_URL line above",
            )
        )
    except Exception as exc:
        results.append(
            ProbeResult("6", "Presigned GET URL (verified from host by `make probe`)", False, str(exc))
        )

    # 7. DeleteObject + DeleteObjects (batch)
    try:
        key = f"{probe_prefix}delete-single.txt"
        client.put_object(Bucket=bucket, Key=key, Body=b"x")
        client.delete_object(Bucket=bucket, Key=key)
        single_gone = not _object_exists(client, bucket, key)

        batch_keys = [f"{probe_prefix}batch-{i}.txt" for i in range(3)]
        for k in batch_keys:
            client.put_object(Bucket=bucket, Key=k, Body=b"x")
        client.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in batch_keys]})
        batch_gone = all(not _object_exists(client, bucket, k) for k in batch_keys)

        ok = single_gone and batch_gone
        results.append(
            ProbeResult(
                "7",
                "DeleteObject + DeleteObjects (batch)",
                ok,
                f"single_gone={single_gone} batch_gone={batch_gone}",
            )
        )
    except Exception as exc:
        results.append(ProbeResult("7", "DeleteObject + DeleteObjects (batch)", False, str(exc)))

    # 8. Key with diacritics and spaces
    try:
        key = f"{probe_prefix}smlouvy/2026/dodatek č. 1.pdf"
        client.put_object(Bucket=bucket, Key=key, Body=b"diakritika")
        fetched = client.get_object(Bucket=bucket, Key=key)["Body"].read()
        ok = fetched == b"diakritika"
        results.append(ProbeResult("8", "Key with diacritics and spaces", ok, f"key={key!r}"))
    except Exception as exc:
        results.append(ProbeResult("8", "Key with diacritics and spaces", False, str(exc)))

    _print_probe_table(results)
    return 0 if all(r.passed for r in results) else 1


def _object_exists(client: S3Client, bucket: str, key: str) -> bool:
    try:
        client.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
            return False
        raise


def main() -> int:
    parser = argparse.ArgumentParser(prog="ftpoc")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("bootstrap", help="Create bucket + install OpenSearch index template")
    subparsers.add_parser("backfill", help="Sweep bucket once, enqueue anything missing from PG")
    subparsers.add_parser("reindex", help="Rebuild the index from S3 sidecars, without calling Tika")
    subparsers.add_parser("s3-probe", help="Verify the S3 backend supports what the pipeline needs")

    args = parser.parse_args()
    settings = Settings()  # type: ignore[call-arg]

    commands = {
        "bootstrap": cmd_bootstrap,
        "backfill": cmd_backfill,
        "reindex": cmd_reindex,
        "s3-probe": cmd_s3_probe,
    }
    return commands[args.command](settings)


if __name__ == "__main__":
    sys.exit(main())
