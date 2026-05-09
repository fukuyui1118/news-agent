import pytest

from news_agent.budget import BudgetConfig, BudgetExceeded, BudgetGuard
from news_agent.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture
def budget(store):
    cfg = BudgetConfig(
        provider="newsapi.ai",
        monthly_cap=10,
        per_cycle_hard_cap=3,
        daily_soft_warning=100,
    )
    return BudgetGuard(config=cfg, store=store)


def test_guard_allows_call_under_caps(budget):
    with budget.guard(endpoint="getArticles", query_name="P1") as record:
        record(article_count=5, http_status=200)
    assert budget.cycle_calls == 1


def test_guard_records_failure_status(budget, store):
    with budget.guard(endpoint="getArticles", query_name="P1") as record:
        record(http_status=500, error="server error")
    rows = store.conn.execute(
        "SELECT http_status, error FROM api_usage WHERE query_name='P1'"
    ).fetchone()
    assert rows[0] == 500
    assert rows[1] == "server error"


def test_per_cycle_hard_cap_blocks(budget):
    for _ in range(3):
        with budget.guard(endpoint="getArticles") as record:
            record(article_count=1, http_status=200)
    with pytest.raises(BudgetExceeded):
        with budget.guard(endpoint="getArticles") as record:
            record(article_count=1, http_status=200)


def test_reset_cycle_zeros_counter(budget):
    with budget.guard(endpoint="getArticles") as record:
        record(article_count=1, http_status=200)
    assert budget.cycle_calls == 1
    budget.reset_cycle()
    assert budget.cycle_calls == 0


def test_monthly_cap_blocks_when_used_up(store):
    cfg = BudgetConfig(monthly_cap=2, per_cycle_hard_cap=10)
    g = BudgetGuard(config=cfg, store=store)
    # Pre-populate api_usage with monthly_cap calls
    for _ in range(2):
        store.record_api_call(
            provider="newsapi.ai", endpoint="getArticles", http_status=200
        )
    with pytest.raises(BudgetExceeded):
        with g.guard(endpoint="getArticles") as record:
            record(article_count=1, http_status=200)


def test_usage_summary_shape(budget, store):
    with budget.guard(endpoint="getArticles") as record:
        record(article_count=10, http_status=200)
    summary = budget.usage_summary()
    assert summary["used_30d"] >= 1
    assert summary["monthly_cap"] == 10
    assert summary["per_cycle_hard_cap"] == 3
