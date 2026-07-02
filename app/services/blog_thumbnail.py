"""Course video thumbnail storage (local disk or R2)."""
from __future__ import annotations

import hashlib
import logging
import re
from io import BytesIO
from pathlib import Path

from app.config import get_settings
from app.services.blog_storage import BlogStorageError, blog_upload_dir
from app.services.r2_storage import R2StorageError, delete_object, r2_configured, upload_stream

logger = logging.getLogger(__name__)

_MAX_THUMB_BYTES = 2 * 1024 * 1024
_ALLOWED_MIME = frozenset({"image/jpeg", "image/png", "image/webp"})
_EXT_BY_MIME = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
_R2_PREFIX = "courses/thumbnails/"
_SAFE_ID = re.compile(r"^[0-9a-zA-Z_-]+$")
_ADMIN_MAX_SIZE = (1280, 720)
_WEBP_QUALITY = 82


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


def _cdn_base_url() -> str:
    cfg = get_settings()
    return (cfg.cdn_base_url or cfg.r2_public_url).strip().rstrip("/")


def resolve_thumbnail_public_url(*, attachment_id: str, stored_name: str) -> str:
    """Build public thumbnail URL: CDN when configured, else API path (optionally absolute)."""
    cdn = _cdn_base_url()
    if cdn and is_r2_thumbnail_key(stored_name):
        return f"{cdn}/{stored_name.lstrip('/')}"

    relative = f"/api/blog/attachments/{attachment_id}/thumbnail"
    api_base = get_settings().api_public_base_url.strip().rstrip("/")
    if api_base:
        return f"{api_base}{relative}"
    return relative


def thumbnail_cache_control() -> str:
    return "public, max-age=86400, stale-while-revalidate=604800"


def thumbnail_etag(*, stored_name: str, size: int, mtime_ns: int | None = None) -> str:
    raw = f"{stored_name}:{size}:{mtime_ns or 0}"
    return hashlib.md5(raw.encode(), usedforsecurity=False).hexdigest()


def _normalize_to_webp(content: bytes, mime_type: str) -> tuple[bytes, str]:
    """Resize and convert uploads to WebP; fall back to original bytes on failure."""
    try:
        from PIL import Image

        with Image.open(BytesIO(content)) as img:
            if img.mode in ("RGBA", "LA", "P"):
                rgba = img.convert("RGBA")
                background = Image.new("RGB", rgba.size, (255, 255, 255))
                background.paste(rgba, mask=rgba.split()[-1])
                rgb = background
            else:
                rgb = img.convert("RGB")
            rgb.thumbnail(_ADMIN_MAX_SIZE, Image.Resampling.LANCZOS)
            for quality in (_WEBP_QUALITY, 72, 60):
                out = BytesIO()
                rgb.save(out, format="WEBP", quality=quality, method=4)
                webp_bytes = out.getvalue()
                if len(webp_bytes) <= _MAX_THUMB_BYTES:
                    return webp_bytes, "image/webp"
            raise BlogThumbnailError("封面图片超过 2MB 上限。")
    except BlogThumbnailError:
        raise
    except Exception as exc:
        logger.warning("WebP conversion failed, storing original image: %s", exc)
        return content, mime_type


def store_thumbnail(*, content: bytes, attachment_id: str, mime_type: str) -> str:
    if not content:
        raise BlogThumbnailError("封面图片为空。")

    normalized_mime = mime_type.split(";", 1)[0].strip().lower()
    if normalized_mime not in _ALLOWED_MIME:
        raise BlogThumbnailError("仅支持 JPEG、PNG 或 WebP 封面。")

    processed_content, processed_mime = _normalize_to_webp(content, normalized_mime)
    if len(processed_content) > _MAX_THUMB_BYTES:
        raise BlogThumbnailError("封面图片超过 2MB 上限。")

    aid = _validate_attachment_id(attachment_id)
    ext = _EXT_BY_MIME[processed_mime]

    if r2_configured():
        key = thumbnail_r2_key(aid, ext=ext)
        try:
            upload_stream(
                key=key,
                body=BytesIO(processed_content),
                content_type=processed_mime,
                content_length=len(processed_content),
            )
        except R2StorageError as exc:
            raise BlogThumbnailError(str(exc)) from exc
        return key

    stored_name = thumbnail_local_name(aid, ext=ext)
    path = blog_upload_dir() / stored_name
    path.write_bytes(processed_content)
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
