"""Cash-conversion estimates and upgrades from saved marketplace observations."""

from decimal import Decimal
import sqlite3

import pytest
from fastapi.testclient import TestClient

from autoscout.domain import Assessment, MarketLookup, MarketQuote, Offer, PricingPolicy, Verdict, assess, project_cash
from autoscout.repository import Repository
from autoscout.settings import Settings
from autoscout.titles import TitleCatalog
from autoscout.web import create_app


def test_projection_separates_order_fee_and_fee_on_withdrawal_principal():
    # Original observation: Sonkwo 16.30, SteamPy ask 20.90. Old estimate was
    # 3.96 / 24.29% because the default omitted the withdrawal fee.
    offer = Offer("澳网公开赛2", "https://www.sonkwo.hk/sku/3490", Decimal("16.30"), ("AO Tennis 2",))
    result = assess(offer, MarketLookup(MarketQuote("AO Tennis 2", (Decimal("20.90"),))),
                    TitleCatalog(), PricingPolicy())
    assert result.target_sell_price == Decimal("20.89")
    assert result.net_profit == Decimal("3.76")
    assert result.roi == Decimal("0.2307")
    assert result.pricing["estimated_sell_fee"] == "0.63"
    assert result.pricing["estimated_wallet_credit"] == "20.26"
    assert result.pricing["estimated_cash_receipt"] == "20.06"
    assert result.pricing["estimated_payout_fee"] == "0.20"
    assert result.verdict == Verdict.PRICE_ONLY

    # Distinguish the correct basis from fee-rate addition and from applying
    # 1% to gross revenue. This contract holds for any permitted fee rates.
    profit, _, detail = project_cash(Decimal("50"), Decimal("100"),
                                    PricingPolicy(fee_rate=Decimal("0.10"), payout_fee_rate=Decimal("0.20")))
    assert profit == Decimal("25.00")
    assert detail["estimated_cash_receipt"] == "75.00"
    assert detail["estimated_payout_fee"] == "15.00"


def test_zero_payout_rate_is_explicit_and_does_not_change_order_fee():
    profit, _, detail = project_cash(Decimal("80"), Decimal("89.99"),
                                    PricingPolicy(payout_fee_rate=Decimal("0")))
    assert profit == Decimal("7.29")
    assert detail["estimated_payout_fee"] == "0.00"
    assert detail["estimated_cash_receipt"] == detail["estimated_wallet_credit"] == "87.29"


def test_rounding_display_roi_cannot_promote_a_below_threshold_candidate():
    offer = Offer("Game", "https://www.sonkwo.hk/sku/1", Decimal("201.00"))
    quote = MarketQuote("Game", (Decimal("211.05"),))
    result = assess(offer, MarketLookup(quote), TitleCatalog(),
                    PricingPolicy(fee_rate=Decimal("0"), payout_fee_rate=Decimal("0")))
    assert result.net_profit == Decimal("10.04")
    assert result.roi == Decimal("0.0500")  # display rounds to 5.00%
    assert result.verdict == Verdict.LOW_MARGIN  # actual ratio is below 5%


@pytest.mark.parametrize("name", ["fee_rate", "payout_fee_rate", "min_profit", "min_roi", "undercut"])
def test_nonfinite_pricing_inputs_are_rejected(name):
    with pytest.raises(ValueError, match="有限数"):
        PricingPolicy(**{name: Decimal("NaN")})


def test_settings_pass_fee_bases_separately(tmp_path):
    settings = Settings(tmp_path, fee_rate=Decimal("0.03"), payout_fee_rate=Decimal("0.01"))
    assert settings.policy.fee_rate == Decimal("0.03")
    assert settings.policy.payout_fee_rate == Decimal("0.01")


def test_legacy_repricing_is_general_and_preserves_quote_time_and_previous_values(tmp_path):
    repo = Repository(tmp_path / "ledger.sqlite3")
    repo.begin_run("old-run", "")
    offer = Offer("Mystic Pillars", "https://www.sonkwo.hk/sku/16151", Decimal("2.90"))
    quote = MarketQuote("Mystic Pillars", (Decimal("3.60"),))
    legacy = Assessment(offer, Verdict.OPPORTUNITY, "名称和版本一致", quote,
                        Decimal("3.59"), Decimal("0.58"), Decimal("0.2000"))
    saved_id = repo.save_assessment("old-run", legacy)
    unmatched_id = repo.save_assessment("old-run", Assessment(
        Offer("Unknown", "https://www.sonkwo.hk/sku/2", Decimal("10")), Verdict.NEEDS_REVIEW, "名称待核对"))
    repo.update_run("old-run", "completed", "finished", 2, 2, 1, 0, finished=True)
    before = next(row for row in repo.assessments() if row["id"] == saved_id)
    policy = PricingPolicy(min_profit=Decimal("0.56"))
    assert repo.reprice_legacy_assessments(policy) == 1
    after = next(row for row in repo.assessments() if row["id"] == saved_id)
    assert after["observed_at"] == before["observed_at"]
    assert after["net_profit"] == "0.55" and after["roi"] == "0.1897"
    assert after["verdict"] == "low_margin"
    assert after["reason"] == before["reason"]
    assert after["pricing"]["previous_estimate"]["net_profit"] == "0.58"
    assert after["pricing"]["reused_quote"] == "true"
    assert next(row for row in repo.assessments() if row["id"] == unmatched_id)["pricing"] is None
    assert repo.runs()[0]["opportunities"] == 0
    assert repo.scan_statistics()["latest_estimated_profit"] == "0.00"
    assert repo.reprice_legacy_assessments(PricingPolicy()) == 0
    assert repo.assessments() == [next(row for row in repo.assessments() if row["id"] == unmatched_id), after]


def test_new_result_retains_rates_and_official_titles_when_configuration_changes(tmp_path):
    repo = Repository(tmp_path / "ledger.sqlite3")
    repo.begin_run("scan", "")
    offer = Offer("澳网公开赛2", "https://www.sonkwo.hk/sku/3490", Decimal("16.30"), ("AO Tennis 2",))
    result = assess(offer, MarketLookup(MarketQuote("AO Tennis 2", (Decimal("20.90"),))),
                    TitleCatalog(), PricingPolicy())
    repo.save_assessment("scan", result)
    repo.update_run("scan", "completed", "finished", 1, 1, 1, 0, finished=True)
    app = create_app(Settings(tmp_path, payout_fee_rate=Decimal("0.02")), repo)
    with TestClient(app) as client:
        row = client.get("/api/assessments").json()[0]
        assert row["pricing"]["payout_fee_rate"] == "0.01"
        assert row["pricing"]["estimated_cash_receipt"] == "20.06"
        assert row["offer_alternate_titles"] == ["AO Tennis 2"]
        assert client.get("/api/config").json()["payout_fee_rate"] == "0.02"


def test_database_without_pricing_columns_is_upgraded_safely(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE assessments (id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,offer_title TEXT NOT NULL,offer_url TEXT NOT NULL,
            buy_price TEXT NOT NULL,market_title TEXT,market_url TEXT,market_price TEXT,
            target_sell_price TEXT,net_profit TEXT,roi TEXT,verdict TEXT NOT NULL,
            reason TEXT NOT NULL,observed_at TEXT NOT NULL)""")
    repo = Repository(path)
    with repo._connect() as db:
        names = {row["name"] for row in db.execute("PRAGMA table_info(assessments)")}
    assert {"pricing", "offer_alternate_titles"} <= names


def test_repricing_cannot_keep_an_opportunity_with_nonpositive_target_price(tmp_path):
    repo = Repository(tmp_path / "ledger.sqlite3")
    repo.begin_run("old", "")
    repo.save_assessment("old", Assessment(
        Offer("Game", "https://www.sonkwo.hk/sku/1", Decimal("1.00")),
        Verdict.OPPORTUNITY, "名称和版本一致", MarketQuote("Game", (Decimal("3.60"),)),
        Decimal("3.59"), Decimal("2.48"), Decimal("2.4800")))
    assert repo.reprice_legacy_assessments(PricingPolicy(undercut=Decimal("4"))) == 1
    row = repo.assessments("old")[0]
    assert row["verdict"] == "needs_review" and row["net_profit"] is None
    assert row["target_sell_price"] is None and row["roi"] is None
    assert row["pricing"]["previous_estimate"]["net_profit"] == "2.48"
    assert repo.runs()[0]["opportunities"] == 0
