"""Demand regressions: asking spreads, seller counters and vanished stock are not sales."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path

import pytest
from autoscout.demand import market_trend, parse_page, scoped_snapshot, summarize_asks, summarize_requests
from autoscout.domain import MarketLookup, MarketQuote, Offer, PricingPolicy, Verdict, assess
from autoscout.products import ProductIdentity
from autoscout.ports import SourceError
from autoscout.repository import Repository
from autoscout.scanner import ScanService
from autoscout.settings import Settings
from autoscout.titles import TitleCatalog


def product():
    return ProductIdentity("steampy", "101", ("Game",), "https://steampy.com/cdkDetail?name=cn&gameId=101",
                           "base", "cn", detail_checked=True)


def snapshot(**changes):
    p = product()
    return {"model": "public_open_requests_and_supply_v1", "product_id": p.product_id,
            "fingerprint": p.fingerprint, "region": "cn", "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "lowest_ask": "100.00", "stock": 100, "sampled_stock": 100, "asks_complete": True,
            "ask_listings": 20, "ask_sampled_listings": 20,
            "requests_complete": True, "open_requests": 0, "best_request_price": None,
            "recent_sales_7d": None, "recent_sales_30d": None, "estimated_sell_days": None, **changes}


def appraisal(demand):
    left = ProductIdentity("sonkwo", "201", ("Game",), "https://www.sonkwo.hk/sku/201", "base", "cn", detail_checked=True)
    offer = Offer("Game", left.url, Decimal("10"), product=left)
    return assess(offer, MarketLookup(MarketQuote("Game", (Decimal("100"),), product().url,
                                                  product=product(), demand=demand)), TitleCatalog(), PricingPolicy())


def request(**changes):
    return {"id": "one", "gameId": "101", "gameName": "Game", "gameNameCn": None,
            "txStatus": "02", "delFlag": 0, "txPrice": 11, "createTime": "2026-10-02 12:00:00", **changes}


def test_huge_spread_without_buyers_is_only_a_price_spread():
    result = appraisal(snapshot())
    assert result.verdict == Verdict.PRICE_ONLY and result.net_profit > 80
    assert result.liquidity["status"] == "no_open_requests"
    assert result.liquidity["recent_sales_7d"] is None
    assert result.liquidity["estimated_sell_days"] is None


def test_unknown_demand_is_distinct_from_verified_empty_requests():
    result = appraisal(snapshot(open_requests=None, requests_complete=False, request_issue="timeout"))
    assert result.verdict == Verdict.PRICE_ONLY
    assert result.liquidity["status"] == "unverified"
    assert appraisal(None).verdict == Verdict.PRICE_ONLY


def test_low_buy_request_can_be_loss_despite_large_asking_profit():
    result = appraisal(snapshot(open_requests=1, best_request_price="9.00"))
    assert result.verdict == Verdict.PRICE_ONLY and result.net_profit > 80
    assert result.liquidity["request_profit"] == "-1.36"
    assert result.liquidity["request_roi"] == "-0.1360"


def test_positive_buy_request_qualifies_with_exact_fees_and_one_unit_only():
    result = appraisal(snapshot(open_requests=500, best_request_price="11.00"))
    assert result.verdict == Verdict.OPPORTUNITY
    assert result.liquidity["request_profit"] == "0.56" and result.liquidity["request_roi"] == "0.0560"
    assert result.liquidity["request_pricing"]["estimated_cash_receipt"] == "10.56"
    assert result.liquidity["request_profit"] != str(Decimal("0.56") * 500)


@pytest.mark.parametrize("changes", [{"region": "us"}, {"product_id": "102"}, {"fingerprint": "other"},
                                     {"observed_at": "not a date"}, {"observed_at": "2020-01-01T00:00:00+00:00"}])
def test_wrong_or_old_evidence_cannot_qualify(changes):
    demand = snapshot(open_requests=1, best_request_price="100", **changes)
    assert not scoped_snapshot(demand, product())
    assert appraisal(demand).verdict == Verdict.PRICE_ONLY


def test_recent_open_requests_are_not_recent_sales():
    observed = "2026-10-02T06:00:00+00:00"
    data = summarize_requests([request(), request(id="old", createTime="2026-08-01 12:00:00"),
                               request(id="closed", txStatus="40")], product(), 3, True, observed)
    assert data["open_requests"] == 2 and data["recent_open_requests_7d"] == 1
    assert "recent_sales_7d" not in data


@pytest.mark.parametrize("changes", [{"gameId": "other"}, {"gameName": "Game Deluxe Edition"},
                                     {"txPrice": "NaN"}, {"createTime": "2030-01-01 12:00:00"}])
def test_request_identity_price_and_time_must_be_consistent(changes):
    with pytest.raises(SourceError):
        summarize_requests([request(**changes)], product(), 1, True, "2026-10-02T06:00:00+00:00")


def test_duplicate_request_and_listing_pagination_rejected():
    with pytest.raises(SourceError, match="重复"):
        summarize_requests([request(), request()], product(), 2, True, "2026-10-02T06:00:00+00:00")
    row = {"saleId": "one", "keyPrice": 12, "stock": 1, "ccy": "CNY", "sold": 9999}
    with pytest.raises(SourceError, match="重复"):
        summarize_asks([row, row], 2, True)
    wrong = {**row, "ccy": "USD"}
    with pytest.raises(SourceError, match="国区"):
        summarize_asks([wrong], 1, True)


def test_seller_success_counters_cannot_become_product_sales():
    rows = [{"saleId": "a", "keyPrice": 12, "stock": 1, "ccy": "CNY", "sold": 100000},
            {"saleId": "b", "keyPrice": 20, "stock": 5, "ccy": "CNY", "sold": 100000}]
    summary, prices = summarize_asks(rows, 100, False)
    assert summary["sampled_stock"] == 6 and summary["stock"] is None
    assert summary["near_lowest_stock"] == 1 and prices[0] == Decimal("12.00")
    assert "sold" not in json.dumps(summary)


def test_exact_pagination_and_partial_stock_coverage():
    payload = {"success": True, "result": {"content": [{"x": 1}], "totalElements": 51, "totalPages": 2, "number": 1}}
    assert parse_page(payload, 2, 50)[1:] == (51, 2)
    with pytest.raises(SourceError, match="页码"):
        parse_page(payload, 1, 50)
    payload["result"]["content"] = []
    with pytest.raises(SourceError, match="不完整"):
        parse_page(payload, 2, 50)


def test_vanished_stock_and_requests_are_not_inferred_sales():
    earlier = snapshot(observed_at="2026-10-01T06:00:00+00:00", lowest_ask="100.00", stock=100, open_requests=9)
    current = snapshot(observed_at="2026-10-02T06:00:00+00:00", lowest_ask="80.00", stock=3, open_requests=2)
    trend = market_trend(current, [earlier, {**earlier, "product_id": "another"}])
    assert trend["observations"] == 2 and trend["hours"] == 24
    assert trend["ask_change_pct"] == "-20.00" and trend["stock_change"] == -97
    assert trend["open_requests_change"] == -7
    assert "不能据此认定卖出" in trend["reason"]
    assert "recent_sales_7d" not in trend and "estimated_sell_days" not in trend
    assert market_trend(current, [earlier, {**earlier, "observed_at": "2026-10-02T05:55:00+00:00"}])["observations"] == 2
    assert market_trend(current, [])['status'] == 'collecting'
    assert market_trend({**current, "asks_complete": False}, [earlier])["stock_change"] is None


def test_public_real_market_regression_fixture():
    fixture = json.loads((Path(__file__).parent / "fixtures/public_steampy_demand.json").read_text(encoding="utf-8"))
    for source in fixture["products"]:
        rows = [{**row, "saleId": str(index)} for index, row in enumerate(source["sale_rows"], 1)]
        summary, prices = summarize_asks(rows, source["sale_total"], len(rows) == source["sale_total"])
        assert summary["lowest_ask"] == str(min(prices))
        p = replace(product(), product_id=source["product_id"], names=tuple(source["names"]))
        wants = [{**row, "id": str(index)} for index, row in enumerate(source["want_rows"], 1)]
        requests = summarize_requests(wants, p, source["want_total"], True, fixture["checked_at"])
        assert requests["open_requests"] == source["want_total"]
        if source["title"] == "澳网公开赛2":
            assert summary["lowest_ask"] == "20.90" and requests["best_request_price"] == "15.00"
        if source["title"] == "秘境之柱":
            assert requests["open_requests"] == 0


def test_repository_keeps_scoped_history_and_request_profit_statistics(tmp_path):
    repo = Repository(tmp_path / "test.sqlite3")
    for run in ("a", "b"):
        repo.begin_run(run, "")
        result = appraisal(snapshot(open_requests=1, best_request_price="11.00"))
        repo.save_assessment(run, result)
        repo.update_run(run, "completed", "finished", 1, 1, 1, 0, finished=True)
    stats = repo.scan_statistics()
    assert stats["latest_estimated_profit"] == "0.56", "Do not add asking-profit or earlier scan profits"
    saved = repo.assessments()[0]
    assert saved["liquidity"]["open_requests"] == 1
    future = snapshot(observed_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds"))
    assert len(repo.market_history(future)) == 2
    assert repo.market_history({**future, "fingerprint": "different"}) == []


def test_old_price_opportunities_reclassified_without_inventing_demand_or_changing_money(tmp_path):
    from autoscout.domain import Assessment
    repo = Repository(tmp_path / "test.sqlite3")
    repo.begin_run("old", "")
    offer = Offer("Game", "https://www.sonkwo.hk/sku/1", Decimal("10"))
    repo.save_assessment("old", Assessment(offer, Verdict.OPPORTUNITY, "old result", MarketQuote("Game", (Decimal("100"),)),
                                          Decimal("99.99"), Decimal("86.03"), Decimal("8.6030"), {"model": "old"}))
    repo.update_run("old", "completed", "finished", 1, 1, 1, 0, finished=True)
    before = repo.assessments()[0]
    assert repo.reclassify_legacy_demand() == 1 and repo.reclassify_legacy_demand() == 0
    after = repo.assessments()[0]
    assert after["verdict"] == "price_only" and after["liquidity"]["status"] == "unverified"
    for key in ("buy_price", "market_price", "net_profit", "roi", "observed_at"):
        assert after[key] == before[key]
    assert repo.scan_statistics()["latest_estimated_profit"] == "0.00"
    assert repo.runs()[0]["opportunities"] == 0
