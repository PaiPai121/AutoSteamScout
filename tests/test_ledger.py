from decimal import Decimal

import pytest

from autoscout.domain import Assessment, MarketQuote, Offer, Verdict
from autoscout.repository import InvalidTradeTransition, Repository


def make_assessment(repo: Repository) -> int:
    repo.begin_run("run-1", "test")
    return repo.save_assessment(
        "run-1",
        Assessment(
            Offer("Game", "https://www.sonkwo.cn/store/1", Decimal("70")),
            Verdict.OPPORTUNITY, "已核对",
            MarketQuote("Game", (Decimal("100"),)), Decimal("99.99"), Decimal("27"), Decimal("0.3857")
        ),
    )


def test_cash_profit_requires_actual_settlement(tmp_path):
    repo = Repository(tmp_path / "db.sqlite3")
    assessment_id = make_assessment(repo)
    trade = repo.purchase(assessment_id, Decimal("72.10"), "purchase-1")
    assert repo.cash_report()["realized_profit"] == "0.00"
    assert repo.cash_report()["capital_tied"] == "72.10"
    repo.transition(trade["id"], "listed", Decimal("100"))
    repo.transition(trade["id"], "sold", Decimal("98"), Decimal("2.94"))
    assert repo.cash_report()["realized_profit"] == "0.00"
    assert repo.cash_report()["awaiting_payout"] == 1
    with pytest.raises(ValueError):
        repo.transition(trade["id"], "settled", Decimal("96.00"))
    repo.transition(trade["id"], "settled", Decimal("94.00"), reference="payout-1")
    report = repo.cash_report()
    assert report["capital_tied"] == "0.00"
    assert report["awaiting_payout"] == 0
    assert report["cash_received"] == "94.00"
    assert report["realized_profit"] == "21.90"
    assert [event["event"] for event in repo.trade_events(trade["id"])] == [
        "purchased", "listed", "sold", "settled"
    ]


def test_invalid_transition_does_not_change_trade(tmp_path):
    repo = Repository(tmp_path / "db.sqlite3")
    trade = repo.purchase(make_assessment(repo), Decimal("70"))
    with pytest.raises(InvalidTradeTransition):
        repo.transition(trade["id"], "settled", Decimal("90"))
    assert repo.trades()[0]["state"] == "purchased"
    with pytest.raises(ValueError, match="已登记"):
        repo.purchase(1, Decimal("70"))
