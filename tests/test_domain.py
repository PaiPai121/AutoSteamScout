from decimal import Decimal

import pytest

from autoscout.domain import MarketLookup, MarketQuote, Offer, PricingPolicy, Verdict, assess, parse_price
from autoscout.titles import TitleCatalog


def test_price_parser_rejects_ambiguous_and_malformed_prices():
    assert parse_price("券后价 ￥1,299.50") == Decimal("1299.50")
    for raw in ("券后 ¥88 原价 ¥120", "...", "0", "¥12.345"):
        with pytest.raises(ValueError):
            parse_price(raw)


def test_product_match_fails_closed_on_wrong_edition_and_unknown_alias():
    catalog = TitleCatalog({"Hollow Knight": ["空洞骑士"]})
    assert catalog.compare("【史低】空洞骑士 Steam激活码 标准版", "Hollow Knight")[0]
    assert not catalog.compare("空洞骑士 标准版", "Hollow Knight 豪华版")[0]
    assert not catalog.compare("空洞骑士 DLC", "Hollow Knight DLC")[0]
    assert not catalog.compare("空洞骑士DLC", "Hollow Knight DLC")[0]
    assert not TitleCatalog().compare("Gold Rush", "Rush Gold Edition")[0]
    assert not TitleCatalog().compare("空洞骑士", "Hollow Knight")[0]
    assert TitleCatalog().compare("Game Deluxe Edition (Steam)", "Game Deluxe Edition")[0]
    assert not TitleCatalog().compare("Game Deluxe Edition (Steam)", "Game Standard Edition")[0]
    assert not TitleCatalog().compare("Game of the Year Edition", "Game")[0]
    assert not TitleCatalog().compare("Game Director's Cut", "Game")[0]


def test_estimated_profit_uses_lowest_actual_quote_and_both_thresholds():
    offer = Offer("Hollow Knight", "https://www.sonkwo.cn/store/1", Decimal("80"))
    quote = MarketQuote("Hollow Knight", (Decimal("100"), Decimal("90"), Decimal("95")))
    result = assess(offer, MarketLookup(quote), TitleCatalog(), PricingPolicy())
    assert result.verdict == Verdict.PRICE_ONLY  # no verified buyer demand in this quote
    assert result.target_sell_price == Decimal("89.99")
    assert result.net_profit == Decimal("6.43")
    assert result.roi == Decimal("0.0804")
    thin = assess(offer, MarketLookup(quote), TitleCatalog(),
                  PricingPolicy(min_roi=Decimal("0.10")))
    assert thin.verdict == Verdict.LOW_MARGIN


def test_no_quote_never_becomes_an_opportunity():
    offer = Offer("Game", "https://www.sonkwo.cn/store/2", Decimal("10"))
    assert assess(offer, MarketLookup(candidates=("Game Deluxe",)), TitleCatalog(), PricingPolicy()).verdict == Verdict.NEEDS_REVIEW
