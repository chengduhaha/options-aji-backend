"""Baidu Netdisk PDF import planning."""
from __future__ import annotations

from app.services.baidu_netdisk_import import (
    BaiduNetdiskFile,
    build_import_plan,
    infer_document_category,
    title_from_pdf_filename,
)


def test_infer_document_category_from_library_path() -> None:
    assert infer_document_category("/基础专属会员资料库/市场及期权分析报告/a.pdf") == "market-report"
    assert infer_document_category("/高级专属会员资料库/期权课程资料/课程文档/a.pdf") == "course"
    assert infer_document_category("/高级专属会员资料库/期权异动/a.pdf") == "unusual-flow"
    assert infer_document_category("/高级专属会员资料库/个股全景分析/NVDA.pdf") == "stock-research"
    assert infer_document_category("/高级专属会员资料库/投资干货/a.pdf") == "investing-method"
    assert infer_document_category("/高级专属会员资料库/期权情报日报/a.pdf") == "options-intel"
    assert infer_document_category("/其他目录/a.pdf") == "general"


def test_title_from_pdf_filename_removes_extension_and_member_suffix() -> None:
    assert (
        title_from_pdf_filename("阿吉生财有道_美股市场及期权报告_20260525_会员完整版.pdf")
        == "阿吉生财有道 美股市场及期权报告 20260525"
    )


def test_build_import_plan_dedupes_existing_and_repeated_files() -> None:
    files = [
        BaiduNetdiskFile(
            fs_id="1",
            path="/基础专属会员资料库/市场及期权分析报告/report-a.pdf",
            filename="report-a.pdf",
            size=100,
            md5="same",
        ),
        BaiduNetdiskFile(
            fs_id="2",
            path="/高级专属会员资料库/市场及期权分析报告/report-a.pdf",
            filename="report-a.pdf",
            size=100,
            md5="same",
        ),
        BaiduNetdiskFile(
            fs_id="3",
            path="/高级专属会员资料库/期权异动/flow.pdf",
            filename="flow.pdf",
            size=200,
            md5="flow-md5",
        ),
    ]

    plan = build_import_plan(files, existing_keys={("flow.pdf", 200)})

    assert [item.file.fs_id for item in plan.items] == ["1"]
    assert plan.import_count == 1
    assert plan.skipped_duplicate_count == 1
    assert plan.skipped_existing_count == 1
    assert plan.import_bytes == 100
    assert plan.items[0].category == "market-report"


def test_build_import_plan_treats_existing_filename_as_imported_even_if_size_differs() -> None:
    files = [
        BaiduNetdiskFile(
            fs_id="1",
            path="/基础专属会员资料库/市场及期权分析报告/report-a.pdf",
            filename="report-a.pdf",
            size=100,
            md5="new-md5",
        )
    ]

    plan = build_import_plan(files, existing_keys={("report-a.pdf", 99)})

    assert plan.import_count == 0
    assert plan.skipped_existing_count == 1


def test_build_import_plan_treats_existing_same_size_as_imported_duplicate_content() -> None:
    files = [
        BaiduNetdiskFile(
            fs_id="1",
            path="/高级专属会员资料库/期权异动/renamed-report.pdf",
            filename="renamed-report.pdf",
            size=788347,
            md5="same-content-new-name",
        )
    ]

    plan = build_import_plan(files, existing_keys={("original-report.pdf", 788347)})

    assert plan.import_count == 0
    assert plan.skipped_existing_count == 1
