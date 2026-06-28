"""Blog PDF file storage on local disk."""
from __future__ import annotations

import re
import uuid
from pathlib import Path

from app.config import get_settings

_MAX_PDF_BYTES = 25 * 1024 * 1024
_SAFE_NAME = re.compile(r"[^a-zA-Z0-9._-]+")


class BlogStorageError(ValueError):
    pass


def blog_upload_dir() -> Path:
    settings = get_settings()
    root = Path(settings.blog_upload_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def sanitize_filename(name: str) -> str:
    base = Path(name).name.strip() or "document.pdf"
    cleaned = _SAFE_NAME.sub("_", base)
    if not cleaned.lower().endswith(".pdf"):
        cleaned = f"{cleaned}.pdf"
    return cleaned[:200]


def store_pdf(*, content: bytes, original_filename: str) -> tuple[str, Path]:
    if not content:
        raise BlogStorageError("PDF 文件为空。")
    if len(content) > _MAX_PDF_BYTES:
        raise BlogStorageError("PDF 超过 25MB 上限。")
    if not content.startswith(b"%PDF"):
        raise BlogStorageError("仅支持 PDF 文件。")

    stored_name = f"{uuid.uuid4().hex}_{sanitize_filename(original_filename)}"
    path = blog_upload_dir() / stored_name
    path.write_bytes(content)
    return stored_name, path


def resolve_pdf_path(stored_name: str) -> Path:
    if not stored_name or ".." in stored_name or "/" in stored_name or "\\" in stored_name:
        raise BlogStorageError("无效的文件名。")
    path = blog_upload_dir() / stored_name
    if not path.is_file():
        raise BlogStorageError("文件不存在。")
    return path


def delete_pdf(stored_name: str) -> None:
    try:
        path = resolve_pdf_path(stored_name)
    except BlogStorageError:
        return
    path.unlink(missing_ok=True)
