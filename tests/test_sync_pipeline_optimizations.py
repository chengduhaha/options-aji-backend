"""Regression tests for scheduled sync pipeline optimizations."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import Any


class _Settings:
    fmp_api_key = "test-fmp-key"
    sync_enabled = True
    sync_timezone = "America/New_York"

    @property
    def sync_watchlist_symbols(self) -> list[str]:
        return ["AAPL", "MSFT"]


class _SingleSymbolSettings(_Settings):
    @property
    def sync_watchlist_symbols(self) -> list[str]:
        return ["AAPL"]


class _ScalarResult:
    def __init__(self, value: object = None):
        self.value = value

    def scalar_one_or_none(self) -> object:
        return self.value


class _FakeSession:
    def __init__(self, *, existing: object = None):
        self.existing = existing
        self.added: list[object] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.executed = 0

    def get(self, _model: object, _key: object) -> object:
        return self.existing

    def execute(self, _stmt: object) -> _ScalarResult:
        self.executed += 1
        return _ScalarResult(self.existing)

    def add(self, row: object) -> None:
        self.added.append(row)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


def test_news_pipeline_refreshes_per_symbol_redis_cache(monkeypatch: Any) -> None:
    from app.sync.pipelines import market_data_sync

    articles = [
        {
            "publishedDate": "2026-05-18T12:00:00Z",
            "url": f"https://example.test/{i}",
            "tickers": ["AAPL"],
            "title": f"News {i}",
        }
        for i in range(25)
    ]
    session = _FakeSession()
    cache_calls: list[tuple[str, object, int]] = []

    monkeypatch.setattr(market_data_sync, "get_settings", lambda: _Settings())
    monkeypatch.setattr(
        market_data_sync,
        "get_fmp_client",
        lambda: SimpleNamespace(get_stock_news=lambda tickers, limit: articles),
    )
    monkeypatch.setattr(market_data_sync, "SessionLocal", lambda: session)
    monkeypatch.setattr(
        market_data_sync,
        "cache_set",
        lambda key, value, ttl: cache_calls.append((key, value, ttl)),
    )

    market_data_sync.sync_news_pipeline()

    assert session.commits == 1
    assert (
        "news:stock:AAPL",
        {"articles": articles[:20], "source": "sync"},
        market_data_sync.TTL_WARM,
    ) in cache_calls
    assert (
        "news:stock:MSFT",
        {"articles": articles[:20], "source": "sync"},
        market_data_sync.TTL_WARM,
    ) in cache_calls


def test_treasury_pipeline_updates_existing_rate_row(monkeypatch: Any) -> None:
    from app.sync.pipelines import market_data_sync

    existing = SimpleNamespace(
        month1=1.0,
        month2=1.0,
        month3=1.0,
        month6=1.0,
        year1=1.0,
        year2=1.0,
        year5=1.0,
        year10=1.0,
        year30=1.0,
        synced_at=None,
    )
    session = _FakeSession(existing=existing)
    row = {
        "date": "2026-05-18",
        "month1": 4.1,
        "month2": 4.2,
        "month3": 4.3,
        "month6": 4.4,
        "year1": 4.5,
        "year2": 4.6,
        "year5": 4.7,
        "year10": 4.8,
        "year30": 4.9,
    }

    monkeypatch.setattr(market_data_sync, "get_settings", lambda: _Settings())
    monkeypatch.setattr(
        market_data_sync,
        "get_fmp_client",
        lambda: SimpleNamespace(get_treasury_rates=lambda from_date: [row]),
    )
    monkeypatch.setattr(market_data_sync, "SessionLocal", lambda: session)
    monkeypatch.setattr(market_data_sync, "cache_set", lambda *args, **kwargs: None)

    market_data_sync.sync_treasury_rates_pipeline()

    assert session.added == []
    assert existing.month1 == 4.1
    assert existing.year30 == 4.9
    assert existing.synced_at is not None
    assert session.commits == 1


def test_analyst_ratings_pipeline_skips_duplicate_company_date(monkeypatch: Any) -> None:
    from app.sync.pipelines import market_data_sync

    session = _FakeSession(existing=object())
    ratings = [{"date": "2026-05-18", "gradingCompany": "Test Bank", "newGrade": "Buy"}]

    monkeypatch.setattr(market_data_sync, "get_settings", lambda: _Settings())
    monkeypatch.setattr(
        market_data_sync,
        "get_fmp_client",
        lambda: SimpleNamespace(get_analyst_ratings=lambda symbol: ratings),
    )
    monkeypatch.setattr(market_data_sync, "SessionLocal", lambda: session)
    monkeypatch.setattr(market_data_sync, "cache_set", lambda *args, **kwargs: None)

    market_data_sync.sync_analyst_ratings_pipeline()

    assert session.executed == 2
    assert session.added == []


def test_company_profile_pipeline_upserts_profile_and_peers(monkeypatch: Any) -> None:
    from app.sync.pipelines import company_profile_sync

    existing = SimpleNamespace(
        company_name=None,
        industry=None,
        sector=None,
        description=None,
        ceo=None,
        employees=None,
        website=None,
        image_url=None,
        ipo_date=None,
        market_cap=None,
        is_etf=False,
        exchange=None,
        country=None,
        raw_json=None,
        synced_at=None,
    )
    session = _FakeSession(existing=existing)

    monkeypatch.setattr(company_profile_sync, "get_settings", lambda: _SingleSymbolSettings())
    monkeypatch.setattr(
        company_profile_sync,
        "get_fmp_client",
        lambda: SimpleNamespace(
            get_profile=lambda symbol: {
                "symbol": symbol,
                "companyName": f"{symbol} Inc",
                "industry": "Software",
                "sector": "Technology",
                "description": "Profile",
                "ceo": "CEO",
                "fullTimeEmployees": "1234",
                "website": "https://example.test",
                "image": "https://example.test/logo.png",
                "ipoDate": "2020-01-02",
                "mktCap": 123456,
                "isEtf": False,
                "exchangeShortName": "NASDAQ",
                "country": "US",
            },
            get_peers=lambda symbol: [f"{symbol}P"],
        ),
    )
    monkeypatch.setattr(company_profile_sync, "SessionLocal", lambda: session)

    company_profile_sync.sync_company_profiles_pipeline()

    assert existing.company_name == "AAPL Inc"
    assert existing.employees == 1234
    assert existing.ipo_date == date(2020, 1, 2)
    assert existing.raw_json["peers"] == ["AAPLP"]
    assert session.commits == 1


def test_social_sentiment_job_is_guarded_by_market_hours(monkeypatch: Any) -> None:
    from app.sync import scheduler

    captured: dict[str, object] = {}

    class FakeScheduler:
        running = False

        def __init__(self, timezone: object):
            self.timezone = timezone
            self.jobs: list[dict[str, object]] = []

        def add_job(self, func: object, trigger: object, **kwargs: object) -> None:
            self.jobs.append({"func": func, "trigger": trigger, **kwargs})
            if kwargs.get("id") == "social_sentiment":
                captured["func"] = func

        def start(self) -> None:
            self.running = True

        def get_jobs(self) -> list[object]:
            return []

    calls: list[str] = []

    monkeypatch.setattr(scheduler, "_scheduler", None)
    monkeypatch.setattr(scheduler, "get_settings", lambda: _Settings())
    monkeypatch.setattr(scheduler, "BackgroundScheduler", FakeScheduler)
    monkeypatch.setattr(scheduler, "_market_hours_guard", lambda: False)
    monkeypatch.setattr(scheduler, "_run_safe", lambda _fn, name: calls.append(name))

    scheduler.start_scheduler()
    func = captured["func"]
    assert callable(func)
    assert func() is False
    assert calls == []


def test_agent_real_data_context_includes_resilient_extra_sources(monkeypatch: Any) -> None:
    from app.agents import user_agent

    class FakeToolkit:
        def frontend_market_bar(self, symbol: str) -> dict[str, object]:
            return {"symbol": symbol, "spot": 100}

        def get_gex(self, symbol: str) -> dict[str, object]:
            return {"symbol": symbol, "netGex": 1, "strikes": []}

        def get_option_chain_full(self, symbol: str) -> dict[str, object]:
            return {"symbol": symbol, "calls": [], "puts": [], "expiration": "2026-06-19"}

        def snapshot_bundle(self, symbol: str) -> dict[str, object]:
            return {"earnings": [{"symbol": symbol}]}

    monkeypatch.setattr(user_agent, "build_default_toolkit", lambda: FakeToolkit())
    monkeypatch.setattr(
        user_agent,
        "get_settings",
        lambda: SimpleNamespace(fmp_api_key="fmp", massive_api_key="massive"),
    )
    monkeypatch.setattr(
        user_agent,
        "get_fmp_client",
        lambda: SimpleNamespace(
            get_price_target_summary=lambda symbol: {},
            get_analyst_ratings=lambda symbol: [],
            get_insider_trades=lambda symbol: [{"symbol": symbol, "filingDate": "2026-05-18"}],
            get_income_statement=lambda symbol: [{"symbol": symbol, "revenue": 1}],
            get_balance_sheet=lambda symbol: [{"symbol": symbol, "cashAndCashEquivalents": 2}],
        ),
    )
    monkeypatch.setattr(
        user_agent,
        "get_massive_client",
        lambda: SimpleNamespace(
            get_option_chain_snapshot=lambda symbol: [{"details": {"ticker": f"O:{symbol}"}}],
        ),
        raising=False,
    )
    monkeypatch.setattr(
        user_agent,
        "_fetch_xpoz_sentiment",
        lambda symbol: SimpleNamespace(
            model_dump=lambda: {"mentions_24h": 10, "sentiment_score": 61, "symbol": symbol}
        ),
        raising=False,
    )

    ctx = user_agent._fetch_real_data_context("AAPL")

    assert ctx["insider_trades"] == [{"symbol": "AAPL", "filingDate": "2026-05-18"}]
    assert ctx["financials"]["income_statement"] == [{"symbol": "AAPL", "revenue": 1}]
    assert ctx["financials"]["balance_sheet"] == [{"symbol": "AAPL", "cashAndCashEquivalents": 2}]
    assert ctx["social_sentiment"] == {"mentions_24h": 10, "sentiment_score": 61, "symbol": "AAPL"}
    assert ctx["massive_option_chain_depth"]["contracts_count"] == 1
