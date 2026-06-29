"""Baidu Netdisk PDF import helpers for the member document library."""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models_blog import BlogAttachmentRow
from app.services.blog_storage import blog_upload_dir, store_pdf

BAIDU_LISTALL_URL = "https://pan.baidu.com/rest/2.0/xpan/multimedia"
DEFAULT_ROOTS = ("/基础专属会员资料库", "/高级专属会员资料库")
DEFAULT_MIN_FREE_BYTES = 2 * 1024 * 1024 * 1024
PDF_DOWNLOAD_CAP_BYTES = 25 * 1024 * 1024

_CATEGORY_MAP = {
    "市场及期权分析报告": "market-report",
    "期权课程资料": "course",
    "期权异动": "unusual-flow",
    "个股全景分析": "stock-research",
    "投资干货": "investing-method",
    "期权情报日报": "options-intel",
}


@dataclass(frozen=True)
class BaiduNetdiskFile:
    fs_id: str
    path: str
    filename: str
    size: int
    md5: str = ""

    @property
    def dedupe_key(self) -> tuple[str, int]:
        return (self.filename, self.size)

    @property
    def content_key(self) -> str:
        return self.md5 or f"{self.filename}:{self.size}"


@dataclass(frozen=True)
class ImportPlanItem:
    file: BaiduNetdiskFile
    title_zh: str
    category: str
    description_zh: str


@dataclass(frozen=True)
class ImportPlan:
    items: list[ImportPlanItem]
    skipped_duplicate_count: int
    skipped_existing_count: int

    @property
    def import_count(self) -> int:
        return len(self.items)

    @property
    def import_bytes(self) -> int:
        return sum(item.file.size for item in self.items)


@dataclass(frozen=True)
class ImportResult:
    plan: ImportPlan
    imported_count: int
    imported_bytes: int
    dry_run: bool


def baidu_access_token_from_env() -> str:
    token = os.environ.get("BAIDU_NETDISK_ACCESS_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BAIDU_NETDISK_ACCESS_TOKEN is required.")
    return token


def infer_document_category(path: str) -> str:
    for marker, category in _CATEGORY_MAP.items():
        if f"/{marker}/" in path or path.endswith(f"/{marker}"):
            return category
    return "general"


def title_from_pdf_filename(filename: str) -> str:
    title = re.sub(r"\.pdf$", "", filename, flags=re.IGNORECASE)
    title = title.replace("_会员完整版", "")
    title = title.replace("_", " ")
    title = title.replace("期权情报日报", "期权情报观察")
    title = re.sub(r"\s+", " ", title).strip()
    return title or filename


def description_from_path(path: str) -> str:
    parts = [part for part in path.strip("/").split("/") if part]
    if len(parts) <= 1:
        return "会员资料库历史文档。"
    folder = " / ".join(parts[:-1])
    return f"会员资料库历史文档，来源分类：{folder}。"


def existing_attachment_keys(session: Session) -> set[tuple[str, int]]:
    rows = session.execute(select(BlogAttachmentRow.original_filename, BlogAttachmentRow.file_size)).all()
    return {(str(name), int(size or 0)) for name, size in rows}


def build_import_plan(
    files: Sequence[BaiduNetdiskFile],
    *,
    existing_keys: set[tuple[str, int]],
) -> ImportPlan:
    seen_content: set[str] = set()
    existing_filenames = {filename for filename, _ in existing_keys}
    existing_sizes = {size for _, size in existing_keys if size > 0}
    items: list[ImportPlanItem] = []
    skipped_duplicate = 0
    skipped_existing = 0

    for file in files:
        if file.dedupe_key in existing_keys or file.filename in existing_filenames or file.size in existing_sizes:
            skipped_existing += 1
            continue
        if file.content_key in seen_content:
            skipped_duplicate += 1
            continue
        seen_content.add(file.content_key)
        items.append(
            ImportPlanItem(
                file=file,
                title_zh=title_from_pdf_filename(file.filename),
                category=infer_document_category(file.path),
                description_zh=description_from_path(file.path),
            )
        )
    return ImportPlan(
        items=items,
        skipped_duplicate_count=skipped_duplicate,
        skipped_existing_count=skipped_existing,
    )


def _headers() -> dict[str, str]:
    return {"User-Agent": "pan.baidu.com"}


def list_baidu_pdfs(
    *,
    token: str,
    roots: Iterable[str] = DEFAULT_ROOTS,
    request_interval_seconds: float = 7.0,
) -> list[BaiduNetdiskFile]:
    files: list[BaiduNetdiskFile] = []
    for root in roots:
        start = 0
        while True:
            response = requests.get(
                BAIDU_LISTALL_URL,
                params={
                    "method": "listall",
                    "path": root,
                    "access_token": token,
                    "recursion": "1",
                    "start": str(start),
                    "limit": "1000",
                    "web": "1",
                },
                headers=_headers(),
                timeout=45,
            )
            payload = response.json()
            if payload.get("errno") not in (0, None):
                raise RuntimeError(f"Baidu listall failed for {root}: {payload}")
            rows = payload.get("list") or []
            for row in rows:
                path = str(row.get("path") or "")
                filename = str(row.get("server_filename") or Path(path).name)
                if row.get("isdir") or not (path.lower().endswith(".pdf") or filename.lower().endswith(".pdf")):
                    continue
                files.append(
                    BaiduNetdiskFile(
                        fs_id=str(row.get("fs_id") or row.get("fsid") or ""),
                        path=path,
                        filename=filename,
                        size=int(row.get("size") or 0),
                        md5=str(row.get("md5") or ""),
                    )
                )
            if not payload.get("has_more"):
                break
            start = int(payload.get("cursor") or (start + len(rows)))
            time.sleep(request_interval_seconds)
    return files


def ensure_disk_space(required_bytes: int, *, min_free_bytes: int = DEFAULT_MIN_FREE_BYTES) -> None:
    free = shutil.disk_usage(blog_upload_dir()).free
    if free - required_bytes < min_free_bytes:
        raise RuntimeError(
            f"Not enough free disk space: need {required_bytes} bytes plus {min_free_bytes} bytes reserve, "
            f"only {free} bytes free."
        )


def _chunks(values: Sequence[str], size: int) -> Iterable[list[str]]:
    for idx in range(0, len(values), size):
        yield list(values[idx : idx + size])


def baidu_download_links(*, token: str, fs_ids: Sequence[str]) -> dict[str, str]:
    links: dict[str, str] = {}
    for chunk in _chunks([fs_id for fs_id in fs_ids if fs_id], 10):
        response = requests.get(
            BAIDU_LISTALL_URL,
            params={
                "method": "filemetas",
                "access_token": token,
                "fsids": json.dumps([int(fs_id) for fs_id in chunk]),
                "dlink": "1",
            },
            headers=_headers(),
            timeout=45,
        )
        payload = response.json()
        if payload.get("errno") not in (0, None):
            raise RuntimeError(f"Baidu filemetas failed: {payload}")
        for row in payload.get("list") or []:
            fs_id = str(row.get("fs_id") or "")
            dlink = str(row.get("dlink") or "")
            if fs_id and dlink:
                links[fs_id] = dlink
    return links


def download_pdf_content(*, token: str, dlink: str, expected_size: int) -> bytes:
    if expected_size > PDF_DOWNLOAD_CAP_BYTES:
        raise RuntimeError(f"PDF exceeds local storage cap: {expected_size} bytes.")
    separator = "&" if "?" in dlink else "?"
    response = requests.get(
        f"{dlink}{separator}access_token={token}",
        headers=_headers(),
        timeout=90,
        allow_redirects=True,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Baidu download failed with HTTP {response.status_code}.")
    content = response.content
    if len(content) > PDF_DOWNLOAD_CAP_BYTES:
        raise RuntimeError(f"Downloaded PDF exceeds local storage cap: {len(content)} bytes.")
    return content


def import_baidu_documents(
    *,
    session: Session,
    token: str,
    roots: Iterable[str] = DEFAULT_ROOTS,
    dry_run: bool = True,
    limit: int | None = None,
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
) -> ImportResult:
    files = list_baidu_pdfs(token=token, roots=roots)
    plan = build_import_plan(files, existing_keys=existing_attachment_keys(session))
    selected = plan.items[:limit] if limit is not None else plan.items
    if selected is not plan.items:
        plan = ImportPlan(
            items=selected,
            skipped_duplicate_count=plan.skipped_duplicate_count,
            skipped_existing_count=plan.skipped_existing_count,
        )
    if dry_run:
        return ImportResult(plan=plan, imported_count=0, imported_bytes=0, dry_run=True)

    ensure_disk_space(plan.import_bytes, min_free_bytes=min_free_bytes)
    links = baidu_download_links(token=token, fs_ids=[item.file.fs_id for item in plan.items])
    imported_count = 0
    imported_bytes = 0
    for item in plan.items:
        dlink = links.get(item.file.fs_id)
        if not dlink:
            raise RuntimeError(f"Missing download link for {item.file.path}.")
        content = download_pdf_content(token=token, dlink=dlink, expected_size=item.file.size)
        stored_name, _ = store_pdf(content=content, original_filename=item.file.filename)
        session.add(
            BlogAttachmentRow(
                post_id=None,
                stored_name=stored_name,
                original_filename=item.file.filename,
                mime_type="application/pdf",
                file_size=len(content),
                title_zh=item.title_zh,
                category=item.category,
                description_zh=item.description_zh,
                is_sample=False,
            )
        )
        imported_count += 1
        imported_bytes += len(content)
    session.commit()
    return ImportResult(plan=plan, imported_count=imported_count, imported_bytes=imported_bytes, dry_run=False)
