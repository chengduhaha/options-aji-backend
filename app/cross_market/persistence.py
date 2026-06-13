"""Persistence helpers — PostgreSQL upserts only (requires async SessionLocal)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.cross_market.models import ArbitrageSignalRecord, EventSnapshotRecord


async def upsert_event_snapshots(session: AsyncSession, events: list[dict]) -> None:
    for event in events:
        stmt = insert(EventSnapshotRecord).values(
            event_id=event["event_id"],
            title_zh=event["title_zh"],
            event_type=event["event_type"],
            event_time=event["event_time"],
            options_probability=event["probabilities"]["options"],
            polymarket_probability=event["probabilities"]["polymarket"],
            social_probability=event["probabilities"]["social"],
            institutional_probability=event["probabilities"]["institutional"],
            consensus_probability=event["consensus"],
            disagreement=event["disagreement"],
            arbitrage_direction=event["arbitrage_direction"],
            raw_payload=event,
            updated_at=datetime.utcnow(),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[EventSnapshotRecord.event_id],
            set_={
                "title_zh": event["title_zh"],
                "event_type": event["event_type"],
                "event_time": event["event_time"],
                "options_probability": event["probabilities"]["options"],
                "polymarket_probability": event["probabilities"]["polymarket"],
                "social_probability": event["probabilities"]["social"],
                "institutional_probability": event["probabilities"]["institutional"],
                "consensus_probability": event["consensus"],
                "disagreement": event["disagreement"],
                "arbitrage_direction": event["arbitrage_direction"],
                "raw_payload": event,
                "updated_at": datetime.utcnow(),
            },
        )
        await session.execute(stmt)
    await session.commit()


async def replace_arbitrage_signals(session: AsyncSession, opportunities: list[dict]) -> None:
    for item in opportunities:
        signal_id = f"signal-{item['event_id']}-{item['arbitrage_direction']}"
        stmt = insert(ArbitrageSignalRecord).values(
            signal_id=signal_id,
            event_id=item["event_id"],
            question=item["question"],
            options_probability=item["options_probability"],
            polymarket_probability=item["polymarket_probability"],
            social_probability=item["social_probability"],
            institutional_probability=item["institutional_probability"],
            consensus_probability=item["consensus_probability"],
            disagreement=item["disagreement"],
            arbitrage_direction=item["arbitrage_direction"],
            confidence_score=item["confidence_score"],
            raw_payload=item,
            created_at=datetime.utcnow(),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[ArbitrageSignalRecord.signal_id],
            set_={
                "question": item["question"],
                "options_probability": item["options_probability"],
                "polymarket_probability": item["polymarket_probability"],
                "social_probability": item["social_probability"],
                "institutional_probability": item["institutional_probability"],
                "consensus_probability": item["consensus_probability"],
                "disagreement": item["disagreement"],
                "arbitrage_direction": item["arbitrage_direction"],
                "confidence_score": item["confidence_score"],
                "raw_payload": item,
                "created_at": datetime.utcnow(),
            },
        )
        await session.execute(stmt)
    await session.commit()
