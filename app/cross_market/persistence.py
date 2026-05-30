"""Persistence helpers — PostgreSQL upserts only (requires async SessionLocal)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.cross_market.models import AgentTraceRecord, ArbitrageSignalRecord, EventSnapshotRecord
from app.cross_market.trace_store import OntologyTrace


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


async def save_trace_record(session: AsyncSession, trace: OntologyTrace) -> None:
    stmt = insert(AgentTraceRecord).values(
        trace_id=trace.trace_id,
        source=trace.source,
        query=trace.query,
        matched_pattern=trace.matched_pattern,
        used_objects=trace.used_objects,
        used_relations=trace.used_relations,
        created_at=datetime.fromisoformat(trace.created_at.replace("Z", "+00:00")),
    )
    stmt = stmt.on_conflict_do_nothing(index_elements=[AgentTraceRecord.trace_id])
    await session.execute(stmt)
    await session.commit()


async def list_recent_trace_records(session: AsyncSession, limit: int) -> list[OntologyTrace]:
    rows = await session.execute(
        select(AgentTraceRecord).order_by(AgentTraceRecord.created_at.desc()).limit(limit)
    )
    output: list[OntologyTrace] = []
    for row in rows.scalars().all():
        output.append(
            OntologyTrace(
                trace_id=row.trace_id,
                source=row.source,
                query=row.query,
                matched_pattern=row.matched_pattern,
                used_objects=row.used_objects,
                used_relations=row.used_relations,
                created_at=row.created_at.isoformat(),
            )
        )
    return output
