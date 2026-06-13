"""Congress table schema compatibility tests."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import sessionmaker

from app.db.models import CongressTradeRow
from app.api.routes.congress import _upsert_trades


def test_congress_trade_model_uses_existing_database_column_names() -> None:
    compiled = str(
        select(CongressTradeRow)
        .where(CongressTradeRow.trade_date.isnot(None))
        .order_by(CongressTradeRow.trade_date.desc())
        .compile(dialect=postgresql.dialect())
    )

    assert "congress_trades.transaction_date" in compiled
    assert "congress_trades.synced_at" in compiled
    assert "congress_trades.trade_date" not in compiled
    assert "congress_trades.ingested_at" not in compiled
    assert "congress_trades.comment" not in compiled


def test_congress_upsert_accepts_fmp_stable_latest_shape() -> None:
    engine = create_engine("sqlite:///:memory:")
    CongressTradeRow.__table__.create(engine)
    TestingSession = sessionmaker(bind=engine)
    row = {
        "symbol": "PTON",
        "transactionDate": "2026-06-05",
        "firstName": "James E Hon",
        "lastName": "Banks",
        "office": "James E Hon Banks",
        "assetDescription": "Peloton Interactive Inc",
        "type": "Sale",
        "amount": "$1,001 - $15,000",
    }

    with TestingSession() as session:
        _upsert_trades(session, [row], "senate")

        saved = session.query(CongressTradeRow).one()
        assert saved.member_name == "James E Hon Banks"
        assert saved.chamber == "senate"
        assert saved.symbol == "PTON"
        assert str(saved.trade_date) == "2026-06-05"
        assert saved.transaction_type == "Sale"
