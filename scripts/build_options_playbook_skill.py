#!/usr/bin/env python3
"""Build agent-ready options playbook from skills/options.html."""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "skills" / "options.html"
OUT_DIR = ROOT / "skills" / "options-playbook"

SECTION_TOPICS: dict[str, str] = {
    "section1": "core_strategies",
    "section2": "payoff_curves",
    "section3": "crisis_management",
    "section4": "greeks_strategies",
    "section4_2": "quant_metrics",
    "section5": "unusual_flow",
}

SECTION_SUMMARIES: dict[str, str] = {
    "section1": "四大核心策略：单边买方、垂直价差、卖方备兑/Iron Condor；强调 DTE 30-60、IV Rank 与行权价 Delta 选择。",
    "section2": "区分到期折线与未到期动态收益曲线；Theta 在到期前 15 天加速衰减，买方宜提前止盈。",
    "section3": "危机拆弹：Long 不摊平、Short 被击穿可 Roll；横盘时 Long 清仓、Short 在 50-70% 利润主动平仓。",
    "section4": "Delta/Gamma/Theta/Vega 博弈视角；日历价差与比例价差等特种结构。",
    "section4_2": "IV Rank 买卖方阈值、Volume vs OI、Market GEX；Expected Move 用于铁鹰 wings 设定。",
    "section5": "异动五步法：市值/权利金过滤、Vol>>OI、Ask 成交、IV 共振；散户跟单改 ATM/价差勿抄虚值。",
}

SECTION_KEYWORDS: dict[str, list[str]] = {
    "section1": ["策略", "Long", "Spread", "卖方", "Iron Condor", "DTE", "IV Rank"],
    "section2": ["收益曲线", "Theta", "Gamma", "到期"],
    "section3": ["止损", "Roll", "拆弹", "横盘", "IV Crush"],
    "section4": ["Greeks", "Delta", "Vega", "Calendar", "Ratio"],
    "section4_2": ["IV Rank", "GEX", "Expected Move", "Volume", "OI", "PCR"],
    "section5": ["异动", "Sweep", "Vol/OI", "跟庄", "五步法"],
}


def _strip_scripts(html: str) -> str:
    return re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)


def _strip_header_nav(html: str) -> str:
    return re.sub(r"<header\b[^>]*>.*?</header>", "", html, flags=re.DOTALL | re.IGNORECASE)


def _mermaid_to_pre(html: str) -> str:
    def repl(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        inner = re.sub(r"<br\s*/?>", "\n", inner, flags=re.IGNORECASE)
        inner = re.sub(r"<[^>]+>", " ", inner)
        inner = re.sub(r"\s+", " ", inner).strip()
        return f'<pre class="mermaid-source" data-note="flowchart-text">{inner}</pre>'

    return re.sub(
        r'<div\s+class="mermaid[^"]*"[^>]*>(.*?)</div>',
        repl,
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )


def _extract_title(section_html: str) -> str:
    m = re.search(r"<h2[^>]*>(.*?)</h2>", section_html, re.DOTALL | re.IGNORECASE)
    if not m:
        return ""
    title = re.sub(r"<[^>]+>", "", m.group(1))
    return re.sub(r"\s+", " ", title).strip()


def _text_excerpt(section_html: str, limit: int = 400) -> str:
    text = re.sub(r"<script\b[^>]*>.*?</script>", "", section_html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style\b[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _annotate_sections(html: str) -> tuple[str, list[dict[str, object]]]:
    pattern = re.compile(
        r'(<section\s+id="(section[^"]+)"[^>]*>)',
        re.IGNORECASE,
    )
    parts = pattern.split(html)
    if len(parts) < 2:
        return html, []

    out_chunks: list[str] = [parts[0]]
    sections_meta: list[dict[str, object]] = []
    i = 1
    while i < len(parts):
        open_tag = parts[i]
        sec_id = parts[i + 1]
        body = parts[i + 2] if i + 2 < len(parts) else ""
        topic = SECTION_TOPICS.get(sec_id, sec_id)
        annotated_open = re.sub(
            r"<section\s+",
            f'<section data-section-id="{sec_id}" data-topic="{topic}" ',
            open_tag,
            count=1,
            flags=re.IGNORECASE,
        )
        title_zh = _extract_title(body)
        summary = SECTION_SUMMARIES.get(sec_id) or _text_excerpt(body, 280)
        sections_meta.append(
            {
                "id": sec_id,
                "topic": topic,
                "title_zh": title_zh,
                "keywords": SECTION_KEYWORDS.get(sec_id, []),
                "summary": summary,
            }
        )
        out_chunks.append(annotated_open)
        out_chunks.append(body)
        i += 3

    return "".join(out_chunks), sections_meta


def _build_playbook_body(raw: str) -> tuple[str, list[dict[str, object]]]:
    html = _strip_scripts(raw)
    html = _strip_header_nav(html)
    html = _mermaid_to_pre(html)
    main_m = re.search(r"<main\b[^>]*>(.*)</main>", html, re.DOTALL | re.IGNORECASE)
    intro_m = re.search(
        r'<section\s+class="bg-gradient[^"]*"[^>]*>.*?</section>',
        html,
        re.DOTALL | re.IGNORECASE,
    )
    main_inner = main_m.group(1) if main_m else html
    intro = intro_m.group(0) if intro_m else ""
    annotated, sections_meta = _annotate_sections(main_inner)
    playbook = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <title>OptionsAji 期权实战教材（Agent）</title>
  <meta name="agent-playbook" content="options-playbook" />
</head>
<body>
  <article id="options-playbook-root">
    {intro}
    {annotated}
  </article>
</body>
</html>"""
    return playbook, sections_meta


def _write_skill_md() -> None:
    content = """---
name: options-playbook
description: 美股期权高级策略、Greeks、Expected Move、IV Rank、异动跟庄五步法。用户问策略选择、风控、异动解读、合约筛选器语境时使用。
---

# 期权实战教材（HTML）

- 正文：`playbook.html`（按 `data-section-id` 阅读）
- 章节索引：`sections.json`

## 章节

- section1: 核心策略（Long / Spread / 卖方 / Iron Condor）
- section2: 收益曲线（到期 vs 未到期）
- section3: 危机拆弹与持仓管理
- section4: 希腊字母与特种策略
- section4_2: 量化指标（IV Rank、GEX、Volume/OI、Expected Move）
- section5: 异动跟庄五步法

## 使用说明

1. 先读 `sections.json` 判断相关章节。
2. 在 `playbook.html` 中定位对应 `section[data-section-id]`。
3. 回答时引用 DTE、IV Rank、Expected Move、Vol/OI 等术语，不构成投资建议。
"""
    (OUT_DIR / "SKILL.md").write_text(content, encoding="utf-8")


def _write_readme() -> None:
    readme = """# Options Playbook Skill

- **源文件（人类浏览）**: `../options.html`
- **生成命令**: `python scripts/build_options_playbook_skill.py`
- **产物**: `playbook.html`, `sections.json`, `SKILL.md`

修改教材内容后请重新运行构建脚本并提交产物（或部署时执行构建）。
"""
    (OUT_DIR / "README.md").write_text(readme, encoding="utf-8")


def main() -> None:
    if not SOURCE.is_file():
        raise SystemExit(f"Source not found: {SOURCE}")

    raw = SOURCE.read_text(encoding="utf-8")
    playbook_html, sections_meta = _build_playbook_body(raw)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "playbook.html").write_text(playbook_html, encoding="utf-8")
    (OUT_DIR / "sections.json").write_text(
        json.dumps({"sections": sections_meta}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _write_skill_md()
    _write_readme()
    print(f"Wrote {OUT_DIR} ({len(sections_meta)} sections)")


if __name__ == "__main__":
    main()
