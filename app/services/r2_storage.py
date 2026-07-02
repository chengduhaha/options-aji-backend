"""Cloudflare R2 object storage (S3-compatible API)."""
from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import BinaryIO

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.client import BaseClient
from botocore.exceptions import ClientError

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


class R2ConfigError(RuntimeError):
    """R2 credentials or bucket configuration is incomplete."""


class R2StorageError(RuntimeError):
    """R2 operation failed."""


@dataclass(frozen=True)
class R2ObjectInfo:
    key: str
    size: int
    etag: str | None = None


def r2_configured(settings: Settings | None = None) -> bool:
    cfg = settings or get_settings()
    return bool(
        cfg.r2_account_id.strip()
        and cfg.r2_access_key_id.strip()
        and cfg.r2_secret_access_key.strip()
        and cfg.r2_bucket_name.strip()
        and cfg.r2_endpoint_url.strip()
    )


def _require_settings(settings: Settings | None = None) -> Settings:
    cfg = settings or get_settings()
    if not r2_configured(cfg):
        raise R2ConfigError(
            "R2 is not configured. Set R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, "
            "R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME, and R2_ENDPOINT_URL."
        )
    return cfg


def get_r2_client(settings: Settings | None = None) -> BaseClient:
    cfg = _require_settings(settings)
    return boto3.client(
        "s3",
        endpoint_url=cfg.r2_endpoint_url.strip(),
        aws_access_key_id=cfg.r2_access_key_id.strip(),
        aws_secret_access_key=cfg.r2_secret_access_key.strip(),
        region_name="auto",
    )


def bucket_name(settings: Settings | None = None) -> str:
    return _require_settings(settings).r2_bucket_name.strip()


def head_bucket(settings: Settings | None = None) -> None:
    client = get_r2_client(settings)
    try:
        client.head_bucket(Bucket=bucket_name(settings))
    except ClientError as exc:
        raise R2StorageError("R2 head_bucket failed.") from exc


def list_objects(*, prefix: str = "", max_keys: int = 1000, settings: Settings | None = None) -> list[R2ObjectInfo]:
    client = get_r2_client(settings)
    bucket = bucket_name(settings)
    try:
        payload = client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=max_keys)
    except ClientError as exc:
        raise R2StorageError(f"R2 list_objects failed for prefix={prefix!r}.") from exc

    rows = payload.get("Contents") or []
    return [
        R2ObjectInfo(
            key=str(row.get("Key") or ""),
            size=int(row.get("Size") or 0),
            etag=str(row.get("ETag") or "").strip('"') or None,
        )
        for row in rows
        if row.get("Key")
    ]


def object_exists(key: str, *, settings: Settings | None = None) -> bool:
    client = get_r2_client(settings)
    try:
        client.head_object(Bucket=bucket_name(settings), Key=key)
        return True
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code") or "")
        if code in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise R2StorageError(f"R2 head_object failed for key={key!r}.") from exc


def upload_stream(
    *,
    key: str,
    body: BinaryIO,
    content_type: str,
    content_length: int | None = None,
    settings: Settings | None = None,
) -> None:
    client = get_r2_client(settings)
    extra_args = {"ContentType": content_type}
    try:
        if content_length is not None:
            client.upload_fileobj(
                body,
                bucket_name(settings),
                key,
                ExtraArgs=extra_args,
                Config=TransferConfig(multipart_threshold=8 * 1024 * 1024),
            )
        else:
            client.upload_fileobj(body, bucket_name(settings), key, ExtraArgs=extra_args)
    except ClientError as exc:
        raise R2StorageError(f"R2 upload failed for key={key!r}.") from exc
    logger.info("R2 upload complete key=%s bytes=%s", key, content_length)


def delete_object(key: str, *, settings: Settings | None = None) -> None:
    client = get_r2_client(settings)
    try:
        client.delete_object(Bucket=bucket_name(settings), Key=key)
    except ClientError as exc:
        raise R2StorageError(f"R2 delete failed for key={key!r}.") from exc


def presigned_get_url(
    key: str,
    *,
    expires_in: int | None = None,
    response_content_disposition: str | None = None,
    settings: Settings | None = None,
) -> str:
    cfg = _require_settings(settings)
    ttl = expires_in if expires_in is not None else cfg.r2_presigned_url_ttl_seconds
    client = get_r2_client(cfg)
    params: dict[str, str] = {"Bucket": bucket_name(cfg), "Key": key}
    if response_content_disposition:
        params["ResponseContentDisposition"] = response_content_disposition
    try:
        return client.generate_presigned_url(
            "get_object",
            Params=params,
            ExpiresIn=ttl,
        )
    except ClientError as exc:
        raise R2StorageError(f"R2 presigned URL failed for key={key!r}.") from exc


def put_test_object(*, settings: Settings | None = None) -> str:
    """Upload a tiny object and return its key (for connectivity checks)."""
    import uuid

    key = f"_healthcheck/{uuid.uuid4().hex}.txt"
    payload = b"options-aji-r2-healthcheck\n"
    client = get_r2_client(settings)
    try:
        client.put_object(
            Bucket=bucket_name(settings),
            Key=key,
            Body=payload,
            ContentType="text/plain",
        )
    except ClientError as exc:
        raise R2StorageError("R2 put_object healthcheck failed.") from exc
    return key


def iter_object_keys(*, prefix: str = "", settings: Settings | None = None) -> Iterator[str]:
    for info in list_objects(prefix=prefix, settings=settings):
        yield info.key


@dataclass(frozen=True)
class R2RangeFetch:
    body: BinaryIO
    content_type: str
    content_length: int
    total_size: int
    range_start: int
    range_end: int
    is_partial: bool


def head_object_size(key: str, *, settings: Settings | None = None) -> int:
    client = get_r2_client(settings)
    try:
        payload = client.head_object(Bucket=bucket_name(settings), Key=key)
    except ClientError as exc:
        raise R2StorageError(f"R2 head_object failed for key={key!r}.") from exc
    return int(payload.get("ContentLength") or 0)


def fetch_object_range(
    key: str,
    *,
    byte_start: int,
    byte_end: int,
    settings: Settings | None = None,
) -> R2RangeFetch:
    client = get_r2_client(settings)
    bucket = bucket_name(settings)
    total_size = head_object_size(key, settings=settings)
    start = max(0, byte_start)
    end = min(total_size - 1, byte_end) if total_size > 0 else byte_end
    if total_size > 0 and start > end:
        raise R2StorageError(f"Invalid byte range for key={key!r}.")
    try:
        response = client.get_object(
            Bucket=bucket,
            Key=key,
            Range=f"bytes={start}-{end}",
        )
    except ClientError as exc:
        raise R2StorageError(f"R2 get_object range failed for key={key!r}.") from exc
    content_length = int(response.get("ContentLength") or (end - start + 1))
    content_type = str(response.get("ContentType") or "application/octet-stream")
    body = response["Body"]
    is_partial = start > 0 or end < total_size - 1
    return R2RangeFetch(
        body=body,
        content_type=content_type,
        content_length=content_length,
        total_size=total_size,
        range_start=start,
        range_end=start + content_length - 1,
        is_partial=is_partial,
    )
