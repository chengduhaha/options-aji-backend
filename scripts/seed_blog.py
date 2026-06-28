"""Seed blog posts with Aji profile copy from ajifinance.manus.space."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone

from sqlalchemy import select

from app.db.models_blog import BlogPostRow
from app.db import models_user  # noqa: F401 — register users table for FK
from app.db.session import SessionLocal


SEED_POSTS: list[dict[str, str]] = [
    {
        "slug": "welcome-aji-finance",
        "category": "insights",
        "title_zh": "阿吉生财有道：为什么做美股期权深度分析",
        "title_en": "Aji Finance: Why Deep US Options Analysis Matters",
        "excerpt_zh": "深耕美股市场，用独家数据分析帮助投资者读懂期权、少走弯路。",
        "excerpt_en": "Deep US options research with proprietary data analysis to help investors learn faster.",
        "tags": "期权,会员,教育",
        "body_zh": """## 关于阿吉

做美股期权，最怕的是自己瞎琢磨、学零散知识踩坑。阿吉专注于美股期权市场研究，致力于通过独特的数据分析视角，帮助投资者更好地理解期权市场的运作机制。

阿吉不仅整合多家顶级期权数据平台的专业数据，更具备独家深度数据分析能力——将海量期权数据转化为阿吉独家的深度分析报告，把复杂的专业信息翻译成普通投资者能看懂、用得上的中文洞察。

与此同时，阿吉专为非专业交易者打造了体系化的期权课程，从基础入门到进阶策略，循序渐进，让每一位有心学习的朋友都能系统掌握期权交易的核心逻辑，少走弯路、把期权学透用活。

## 三大核心优势

1. **数据优势** — 长年订阅 Market Chameleon、SpotGamma、Unusual Whales 等顶级平台，整合多维度专业期权数据。
2. **独家深度分析** — 将专业数据融合市场洞察，生成中文深度分析报告。
3. **期权精品课程** — 专为非专业交易者设计的体系化课程，从入门到进阶。

## 重要提示

- 本文内容仅为知识分享、教学交流使用，不构成任何投资建议、买卖邀约或操作指导。
- 美股、期权均属于高风险投资品种，过往业绩不代表未来收益，市场有风险，投资需谨慎。
- 请投资者独立决策、自负盈亏，理性投资，量力而行。
""",
        "body_en": """## About Aji

Trading US options without structured research often leads to costly mistakes. Aji focuses on options market research and translates professional datasets into actionable Chinese insights for retail learners.

## Three pillars

1. **Data access** — Curated feeds from Market Chameleon, SpotGamma, Unusual Whales, and more.
2. **Proprietary analysis** — Daily deep-dive reports beyond raw dashboards.
3. **Structured courses** — From basics to advanced strategies for non-professional traders.

## Disclaimer

Educational content only—not investment advice. Options involve substantial risk.
""",
    },
    {
        "slug": "member-sample-reports",
        "category": "membership",
        "title_zh": "会员资料示例：每日深度报告长什么样",
        "title_en": "Member Sample: What Daily Reports Look Like",
        "excerpt_zh": "想先看看会员资料长什么样？这里介绍基础档与高级档会员可获得的报告类型。",
        "excerpt_en": "Preview the report types included in Basic and Premium membership tiers.",
        "tags": "会员,报告,示例",
        "body_zh": """## 基础档会员（¥298/月）

- 每日《美股市场及期权深度分析报告》
- 不定期《美股市场期权异动报告》
- 每周《基础会员期权课程》
- 不定期盘前推送《美股市场盘前前瞻 Q&A》
- 不定期《美股投资干货》分享

## 高级档会员（¥598/月）

在基础档基础上增加：

- **每日**《美股市场期权异动报告》
- 每周《高级会员专属期权课程》
- 每周 1 次公司全景分析
- **每日**盘前推送《美股市场盘前前瞻 Q&A》
- 每日盘中操作思路与策略实时分享
- 期权&投资问题，群主答疑

## 示例文档

会员专属 PDF 报告可通过本博客附件下载查看（管理员上传后展示）。

> 本平台仅提供数据分析和教育内容，不构成投资建议。
""",
        "body_en": """## Basic tier (¥298/mo)

- Daily US market & options deep-dive report
- Occasional unusual activity report
- Weekly foundational options course
- Occasional pre-market Q&A brief

## Premium tier (¥598/mo)

Adds daily unusual activity, advanced courses, weekly company panorama, daily pre-market Q&A, intraday strategy notes, and direct Q&A.

Sample PDFs appear as blog attachments when uploaded by admin.

> Educational content only—not investment advice.
""",
    },
]


def seed_blog_posts(*, force: bool = False) -> int:
    session = SessionLocal()
    created = 0
    now = datetime.now(timezone.utc)
    try:
        for item in SEED_POSTS:
            exists = session.execute(
                select(BlogPostRow.id).where(BlogPostRow.slug == item["slug"])
            ).scalar_one_or_none()
            if exists and not force:
                continue
            if exists and force:
                row = session.get(BlogPostRow, exists)
                if row is None:
                    continue
                for key, value in item.items():
                    setattr(row, key, value)
                row.status = "published"
                row.published_at = now
            else:
                row = BlogPostRow(
                    **item,
                    status="published",
                    published_at=now,
                )
                session.add(row)
            created += 1
        session.commit()
    finally:
        session.close()
    return created


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed blog posts")
    parser.add_argument("--force", action="store_true", help="Update existing seed slugs")
    args = parser.parse_args()
    count = seed_blog_posts(force=args.force)
    print(f"Seeded {count} blog post(s)")


if __name__ == "__main__":
    main()
