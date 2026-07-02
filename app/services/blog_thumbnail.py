"""Course video thumbnail storage (local disk or R2)."""
from __future__ import annotations

import re
from io import BytesIO
from pathlib import Path

from app.services.blog_storage import BlogStorageError, blog_upload_dir
from app.services.r2_storage import R2StorageError, delete_object, r2_configured, upload_stream

_MAX_THUMB_BYTES = 2 * 1024 * 1024
_ALLOWED_MIME = frozenset({"image/jpeg", "image/png", "image/webp"})
_EXT_BY_MIME = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
_R2_PREFIX = "courses/thumbnails/"
_SAFE_ID = re.compile(r"^[0-9a-zA-Z_-]+$")


class BlogThumbnailError(ValueError):
    pass


def _validate_attachment_id(attachment_id: str) -> str:
    value = attachment_id.strip()
    if not value or not _SAFE_ID.match(value):
        raise BlogThumbnailError("无效的附件 ID。")
    return value[:120]


def thumbnail_r2_key(attachment_id: str, *, ext: str) -> str:
    return f"{_R2_PREFIX}{attachment_id}{ext}"


def thumbnail_local_name(attachment_id: str, *, ext: str) -> str:
    return f"thumb_{attachment_id}{ext}"


def is_r2_thumbnail_key(stored_name: str) -> bool:
    return stored_name.startswith(_R2_PREFIX)


def store_thumbnail(*, content: bytes, attachment_id: str, mime_type: str) -> str:
    if not content:
        raise BlogThumbnailError("封面图片为空。")
    if len(content) > _MAX_THUMB_BYTES:
        raise BlogThumbnailError("封面图片超过 2MB 上限。")
    normalized_mime = mime_type.split(";", 1)[0].strip().lower()
    if normalized_mime not in _ALLOWED_MIME:
        raise BlogThumbnailError("仅支持 JPEG、PNG 或 WebP 封面。")

    aid = _validate_attachment_id(attachment_id)
    ext = _EXT_BY_MIME[normalized_mime]

    if r2_configured():
        key = thumbnail_r2_key(aid, ext=ext)
        try:
            upload_stream(
                key=key,
                body=BytesIO(content),
                content_type=normalized_mime,
                content_length=len(content),
            )
        except R2StorageError as exc:
            raise BlogThumbnailError(str(exc)) from exc
        return key

    stored_name = thumbnail_local_name(aid, ext=ext)
    path = blog_upload_dir() / stored_name
    path.write_bytes(content)
    return stored_name


def resolve_thumbnail_path(stored_name: str) -> Path:
    if not stored_name or is_r2_thumbnail_key(stored_name):
        raise BlogStorageError("无效的封面文件名。")
    if ".." in stored_name or "/" in stored_name or "\\" in stored_name:
        raise BlogStorageError("无效的封面文件名。")
    path = blog_upload_dir() / stored_name
    if not path.is_file():
        raise BlogStorageError("封面不存在。")
    return path


def delete_thumbnail(stored_name: str) -> None:
    if not stored_name:
        return
    if is_r2_thumbnail_key(stored_name):
        if not r2_configured():
            return
        try:
            delete_object(stored_name)
        except R2StorageError:
            return
        return
    try:
        resolve_thumbnail_path(stored_name).unlink(missing_ok=True)
    except BlogStorageError:
        return


def thumbnail_mime_type(stored_name: str) -> str:
    lower = stored_name.lower()
    if lower.endswith(".png"):
        return "image/png"
    if lower.endswith(".webp"):
        return "image/webp"
    return "image/jpeg"
