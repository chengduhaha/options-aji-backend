"""Generate and refresh Chinese bios for active Congress members."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, select

from app.config import get_settings
from app.db.models import CongressMemberProfileRow, CongressTradeRow
from app.db.session import SessionLocal
from app.services.llm_router import has_llm_provider, post_chat_completions_with_fallback

logger = logging.getLogger(__name__)
_PROFILE_TTL_DAYS = 7


def _trade_summary_for_member(session, member_name: str, chamber: str) -> str:
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=180)
    rows = session.execute(
        select(CongressTradeRow)
        .where(
            and_(
                CongressTradeRow.member_name == member_name,
                CongressTradeRow.chamber == chamber,
                CongressTradeRow.trade_date >= cutoff,
            )
        )
        .order_by(CongressTradeRow.trade_date.desc())
        .limit(12)
    ).scalars().all()
    parts: list[str] = []
    for row in rows:
        parts.append(
            f"{row.trade_date} {row.transaction_type} {row.symbol} {row.amount_range or ''}".strip()
        )
    return "; ".join(parts) if parts else "近期无公开交易披露"


def _generate_bio(member_name: str, chamber: str, trades_summary: str) -> dict[str, str]:
    cfg = get_settings()
    chamber_zh = "美国参议院" if chamber == "senate" else "美国众议院"
    prompt = (
        f"请用中文为美国国会议员撰写一段简洁的个人介绍（80-150字），面向中国美股投资者。\n"
        f"姓名：{member_name}\n议院：{chamber_zh}\n近期交易披露摘要：{trades_summary}\n\n"
        "返回 JSON：{\"bio_zh\":\"...\",\"party\":\"党派或未知\",\"state\":\"州或未知\","
        "\"committee\":\"委员会或未知\",\"notable_trades_summary\":\"一句话交易特点\"}"
    )
    payload: dict[str, object] = {
        "temperature": 0.3,
        "max_tokens": 400,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
    }
    try:
        data, _provider = post_chat_completions_with_fallback(payload, cfg=cfg, source="congress_profile")
        choices = data.get("choices") if isinstance(data, dict) else None
        if isinstance(choices, list) and choices:
            message = choices[0].get("message") if isinstance(choices[0], dict) else None
            text = str(message.get("content") or "").strip() if isinstance(message, dict) else ""
        else:
            text = ""
    except Exception as exc:
        logger.warning("congress profile LLM call failed: %s", exc)
        text = ""
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(text[start : end + 1])
            if isinstance(parsed, dict):
                return {
                    "bio_zh": str(parsed.get("bio_zh") or "").strip(),
                    "party": str(parsed.get("party") or "").strip() or None,
                    "state": str(parsed.get("state") or "").strip() or None,
                    "committee": str(parsed.get("committee") or "").strip() or None,
                    "notable_trades_summary": str(parsed.get("notable_trades_summary") or "").strip() or None,
                }
        except json.JSONDecodeError:
            pass
    return {
        "bio_zh": text[:500] if text else f"{member_name}，{chamber_zh}议员。",
        "party": None,
        "state": None,
        "committee": None,
        "notable_trades_summary": trades_summary[:200],
    }


def refresh_congress_member_profiles_pipeline(*, limit: int = 20) -> None:
    if not has_llm_provider():
        logger.debug("No LLM provider, skip congress member profile refresh")
        return

    session = SessionLocal()
    refreshed = 0
    try:
        members = session.execute(
            select(CongressTradeRow.member_name, CongressTradeRow.chamber, func.max(CongressTradeRow.trade_date))
            .group_by(CongressTradeRow.member_name, CongressTradeRow.chamber)
            .order_by(func.max(CongressTradeRow.trade_date).desc())
            .limit(limit * 3)
        ).all()

        cutoff = datetime.now(timezone.utc) - timedelta(days=_PROFILE_TTL_DAYS)
        for member_name, chamber, _ in members:
            if refreshed >= limit:
                break
            existing = session.get(CongressMemberProfileRow, (member_name, chamber))
            if existing and existing.updated_at and existing.updated_at > cutoff:
                continue
            trades_summary = _trade_summary_for_member(session, member_name, chamber)
            try:
                bio = _generate_bio(member_name, chamber, trades_summary)
            except Exception as exc:
                logger.warning("congress profile LLM failed member=%s: %s", member_name, exc)
                continue
            if existing:
                existing.bio_zh = bio.get("bio_zh")
                existing.party = bio.get("party")
                existing.state = bio.get("state")
                existing.committee = bio.get("committee")
                existing.notable_trades_summary = bio.get("notable_trades_summary")
                existing.updated_at = datetime.now(timezone.utc)
            else:
                session.add(
                    CongressMemberProfileRow(
                        member_name=member_name,
                        chamber=chamber,
                        bio_zh=bio.get("bio_zh"),
                        party=bio.get("party"),
                        state=bio.get("state"),
                        committee=bio.get("committee"),
                        notable_trades_summary=bio.get("notable_trades_summary"),
                    )
                )
            session.commit()
            refreshed += 1
    finally:
        session.close()

    logger.info("congress member profiles refreshed: %d", refreshed)
