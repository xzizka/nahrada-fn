from __future__ import annotations

import io
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from boto3.s3.transfer import TransferConfig
from botocore.exceptions import ClientError

if TYPE_CHECKING:
    # Stub-only dev dependency - see clients.py for why this is safe here.
    from mypy_boto3_s3.client import S3Client


@dataclass(frozen=True)
class ObjectSummary:
    key: str
    etag: str
    size_bytes: int


@dataclass(frozen=True)
class ObjectInfo:
    content_length: int
    content_type: str | None
    etag: str
    metadata: dict[str, str]


def sidecar_key(sha256: str, text_prefix: str) -> str:
    return f"{text_prefix}{sha256}.txt"


class DocumentStore:
    """The only component that speaks to the object store.

    Uses plain boto3 S3 calls exclusively - swapping the backend (RustFS,
    Ceph RGW, SeaweedFS, AWS S3) is a config change to the two endpoint
    settings, never a change to this class.
    """

    def __init__(self, client: S3Client, presign_client: S3Client, bucket: str) -> None:
        self._client = client
        self._presign_client = presign_client
        self._bucket = bucket

    @property
    def bucket(self) -> str:
        return self._bucket

    def ensure_bucket(self) -> None:
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except ClientError as exc:
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status != 404:
                raise
            self._client.create_bucket(Bucket=self._bucket)

    def put_object(
        self,
        key: str,
        body: bytes,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> str:
        """Uploads via the transfer manager so large files use multipart
        automatically, then reads back the ETag with a HeadObject."""
        extra_args: dict[str, str | dict[str, str]] = {}
        if content_type:
            extra_args["ContentType"] = content_type
        if metadata:
            extra_args["Metadata"] = metadata

        self._client.upload_fileobj(
            io.BytesIO(body),
            self._bucket,
            key,
            ExtraArgs=extra_args or None,
            Config=TransferConfig(multipart_threshold=8 * 1024 * 1024),
        )
        return self.head_object(key).etag

    def get_object(self, key: str) -> bytes:
        response = self._client.get_object(Bucket=self._bucket, Key=key)
        return response["Body"].read()

    def head_object(self, key: str) -> ObjectInfo:
        response = self._client.head_object(Bucket=self._bucket, Key=key)
        return ObjectInfo(
            content_length=response["ContentLength"],
            content_type=response.get("ContentType"),
            etag=response["ETag"].strip('"'),
            metadata=response.get("Metadata", {}),
        )

    def object_exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except ClientError as exc:
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status == 404:
                return False
            raise

    def put_text_sidecar(
        self, sha256: str, text: str, tika_version: str, extracted_at: datetime, text_prefix: str
    ) -> str:
        key = sidecar_key(sha256, text_prefix)
        self.put_object(
            key,
            text.encode("utf-8"),
            content_type="text/plain; charset=utf-8",
            metadata={
                "tika-version": tika_version,
                "extracted-at": extracted_at.astimezone(UTC).isoformat(),
            },
        )
        return key

    def sidecar_exists(self, sha256: str, text_prefix: str) -> bool:
        return self.object_exists(sidecar_key(sha256, text_prefix))

    def get_text_sidecar(self, sha256: str, text_prefix: str) -> str:
        return self.get_object(sidecar_key(sha256, text_prefix)).decode("utf-8")

    def get_sidecar_info(self, sha256: str, text_prefix: str) -> ObjectInfo:
        return self.head_object(sidecar_key(sha256, text_prefix))

    def list_objects(self, prefix: str) -> Iterator[ObjectSummary]:
        continuation_token: str | None = None
        while True:
            kwargs: dict[str, str | int] = {"Bucket": self._bucket, "Prefix": prefix, "MaxKeys": 1000}
            if continuation_token:
                kwargs["ContinuationToken"] = continuation_token
            response = self._client.list_objects_v2(**kwargs)  # type: ignore[arg-type]
            for obj in response.get("Contents", []):
                yield ObjectSummary(
                    key=obj["Key"], etag=obj["ETag"].strip('"'), size_bytes=obj["Size"]
                )
            if not response.get("IsTruncated"):
                break
            continuation_token = response.get("NextContinuationToken")

    def presigned_url(self, key: str, expires_in_seconds: int) -> str:
        return self._presign_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=expires_in_seconds,
        )

    def delete_object(self, key: str) -> None:
        self._client.delete_object(Bucket=self._bucket, Key=key)
