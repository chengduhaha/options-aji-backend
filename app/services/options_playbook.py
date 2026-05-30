"""Load and select excerpts from options-playbook HTML skill."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

PLAYBOOK_DIR = Path(__file__).resolve().parents[2] / "skills" / "options-playbook"
SECTIONS_FILE = PLAYBOOK_DIR / "sections.json"
PLAYBOOK_HTML = PLAYBOOK_DIR / "playbook.html"

MAX_BLOB_CHARS = 12_000

DirectionKind = Literal["bull", "bear", "neutral"]
AgentModeKind = Literal["fast", "analysis", "strategy", "mvp"]


@dataclass(frozen=True)
class PlaybookSection:
    id: str
    topic: str
    title_zh: str
    keywords: tuple[str, ...]
    summary: str


@dataclass(frozen=True)
class PlaybookHint:
    topic: str
    bullets: tuple[str, ...]


_HINTS: dict[str, tuple[str, ...]] = {
    "expected_move": (
        "Expected Move = ATM Call mid + ATM Put mid（跨式）÷ 现价，得到隐含波动幅度。",
        "Iron Condor 等波动率中性策略常将 wings 开在 Expected Move 之外。",
        "财报前 IV 抬升时，跨式隐含波动往往偏宽，需结合 IV Rank 判断买方/卖方环境。",
    ),
    "unusual_flow": (
        "异动五步法：市值>200亿、单笔权利金>50万、14<DTE<60 过滤噪音。",
        "当日 Volume >> 昨日 OI 且成交价在 Ask 侧，更似主力新开仓。",
        "散户跟单勿抄极虚值行权价，优先 ATM 或轻微 ITM / 看涨价差。",
    ),
    "screener": (
        "合约筛选器按方向+流动性筛选，不是异动榜；结合 DTE、IV、量/OI 阅读。",
        "买方优选 DTE 30-60；IV Rank < 30% 偏买方，> 70% 偏卖方。",
        "解读需对照大盘 regime 与 Expected Move 风险尺。",
    ),
}


def _load_sections_index() -> list[PlaybookSection]:
    if not SECTIONS_FILE.is_file():
        return []
    data = json.loads(SECTIONS_FILE.read_text(encoding="utf-8"))
    rows = data.get("sections") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    out: list[PlaybookSection] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        sid = str(row.get("id") or "").strip()
        if not sid:
            continue
        kw = row.get("keywords")
        keywords = tuple(str(k) for k in kw) if isinstance(kw, list) else ()
        out.append(
            PlaybookSection(
                id=sid,
                topic=str(row.get("topic") or sid),
                title_zh=str(row.get("title_zh") or sid),
                keywords=keywords,
                summary=str(row.get("summary") or ""),
            )
        )
    return out


def list_sections() -> list[PlaybookSection]:
    return _load_sections_index()


def _extract_section_html(section_id: str) -> str:
    if not PLAYBOOK_HTML.is_file():
        return ""
    html = PLAYBOOK_HTML.read_text(encoding="utf-8")
    pattern = re.compile(
        rf'<section[^>]*data-section-id="{re.escape(section_id)}"[^>]*>(.*?)</section>',
        re.DOTALL | re.IGNORECASE,
    )
    m = pattern.search(html)
    if not m:
        pattern2 = re.compile(
            rf'<section[^>]*\bid="{re.escape(section_id)}"[^>]*>(.*?)</section>',
            re.DOTALL | re.IGNORECASE,
        )
        m = pattern2.search(html)
    return m.group(0) if m else ""


def get_section_html(section_id: str, max_chars: int = 8000) -> str:
    chunk = _extract_section_html(section_id)
    if not chunk:
        return ""
    if len(chunk) <= max_chars:
        return chunk
    return chunk[: max_chars - 20] + "\n<!-- truncated -->"


def select_sections_for_context(
    *,
    direction: DirectionKind | None = None,
    iv_rank: float | None = None,
    unusual_count: int = 0,
    mode: AgentModeKind = "mvp",
) -> list[str]:
    if mode == "strategy":
        ids = ["section1", "section2", "section3"]
    elif mode == "analysis":
        ids = ["section4", "section4_2", "section5"]
    elif mode == "fast":
        return []
    else:
        ids = ["section1", "section4", "section4_2"]

    if unusual_count > 0 and "section5" not in ids:
        ids.append("section5")

    if iv_rank is not None:
        if iv_rank >= 70 and "section1" not in ids:
            ids.insert(0, "section1")
        if iv_rank <= 30 and "section4_2" not in ids:
            ids.append("section4_2")

    if direction in ("bull", "bear") and "section1" not in ids:
        ids.insert(0, "section1")

    seen: set[str] = set()
    ordered: list[str] = []
    for sid in ids:
        if sid not in seen:
            seen.add(sid)
            ordered.append(sid)
    return ordered


def build_playbook_context_blob(
    section_ids: list[str],
    *,
    max_chars: int = MAX_BLOB_CHARS,
) -> str:
    if not section_ids:
        return ""
    parts: list[str] = []
    per = max(1200, max_chars // max(len(section_ids), 1))
    for sid in section_ids:
        meta = next((s for s in list_sections() if s.id == sid), None)
        title = meta.title_zh if meta else sid
        summary = meta.summary if meta else ""
        body = get_section_html(sid, max_chars=per)
        block = f"### {title}\n{summary}\n{body}".strip()
        if block:
            parts.append(block)
    blob = "\n\n---\n\n".join(parts)
    if len(blob) <= max_chars:
        return blob
    return blob[: max_chars - 24] + "\n\n<!-- playbook truncated -->"


def build_fast_summary_blob(*, max_chars: int = 1200) -> str:
    lines = [f"- {s.title_zh}: {s.summary}" for s in list_sections()]
    blob = "【教材章节摘要】\n" + "\n".join(lines)
    if len(blob) <= max_chars:
        return blob
    return blob[: max_chars - 16] + "\n<!-- truncated -->"


def get_playbook_hints(topic: str) -> PlaybookHint:
    key = topic.strip().lower().replace("-", "_")
    bullets = _HINTS.get(key, _HINTS.get("screener", ()))
    return PlaybookHint(topic=key, bullets=bullets)


def playbook_skill_path() -> str:
    return str(PLAYBOOK_DIR)


def rule_snippets_for_mvp(iv_rank: float | None, unusual_count: int) -> str:
    parts: list[str] = []
    if iv_rank is not None:
        if iv_rank >= 70:
            parts.append("IV Rank 偏高：教材建议偏卖方/收权利金思路，注意均值回归。")
        elif iv_rank <= 30:
            parts.append("IV Rank 偏低：教材建议偏买方或低 IV 环境布局。")
    if unusual_count > 0:
        parts.append("存在异动合约：可参考教材五步法（Vol/OI、Ask 成交、勿抄极虚值）。")
    if not parts:
        parts.append("结合 DTE 30-60 与 Expected Move 作为风险尺，客观描述环境。")
    return " ".join(parts)
