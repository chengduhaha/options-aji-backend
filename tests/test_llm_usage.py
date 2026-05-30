from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Base
from app.services.llm_usage import record_llm_usage, summarize_llm_usage


def _session() -> Session:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    return TestingSessionLocal()


def test_record_and_summarize_llm_usage_tokens_and_cost() -> None:
    session = _session()
    try:
        record_llm_usage(
            session,
            provider="openrouter",
            model="deepseek/deepseek-v4-flash",
            source="market_summary",
            success=True,
            input_tokens=1000,
            output_tokens=500,
            cost_usd=0.0123,
        )
        record_llm_usage(
            session,
            provider="xiaomi",
            model="mimo-v2.5",
            source="mvp_war_room",
            success=False,
            input_tokens=100,
            output_tokens=0,
            error_code="401",
        )

        summary = summarize_llm_usage(session)

        assert summary["total"]["calls"] == 2
        assert summary["total"]["success_calls"] == 1
        assert summary["total"]["failed_calls"] == 1
        assert summary["total"]["input_tokens"] == 1100
        assert summary["total"]["output_tokens"] == 500
        assert summary["total"]["total_tokens"] == 1600
        assert summary["total"]["cost_usd"] == 0.0123
        assert summary["by_provider"]["openrouter"]["total_tokens"] == 1500
        assert summary["by_provider"]["xiaomi"]["failed_calls"] == 1
    finally:
        session.close()
