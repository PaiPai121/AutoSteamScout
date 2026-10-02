from decimal import Decimal
from dataclasses import replace

from autoscout.browser import parse_sonkwo_payload
from autoscout.domain import MarketLookup, MarketQuote, PricingPolicy, Verdict, assess
from autoscout.titles import TitleCatalog


def test_sonkwo_current_sku_contract_uses_cash_price_and_official_english_name():
    def sku(sku_id, **overrides):
        item = {
            "id": sku_id, "skuNames": {"chs": "恶魔轮盘", "en": "Buckshot Roulette"},
            "salePrice": 6.0, "specialPrice": [{"price": 4.99}],
            "showCouponPrice": True, "keyType": 0, "region": "cn",
            "priceStatus": 1, "supportCashPay": True, "status": 0,
            "onOffReason": 1001, "kind": 0,
        }
        item.update(overrides)
        return item

    payload = {"success": True, "data": {"list": [
        sku(27591), sku(2, supportCashPay=False), sku(3, keyType=1),
        sku(4, status=3), sku(5, priceStatus=2),
    ]}}
    batch = parse_sonkwo_payload(payload, "", "lowest")
    assert len(batch.offers) == 1
    offer = batch.offers[0]
    assert offer.url == "https://www.sonkwo.hk/sku/27591"
    assert offer.cost == Decimal("6.00")  # no conditional coupon assumed
    assert offer.alternate_titles == ("Buckshot Roulette",)
    verified = replace(offer, product=replace(offer.product, detail_checked=True))
    result = assess(verified, MarketLookup(MarketQuote("Buckshot Roulette", (Decimal("10"),))),
                    TitleCatalog(), PricingPolicy())
    assert result.verdict == Verdict.PRICE_ONLY
    assert "官方多语言" in result.reason
    assert len(parse_sonkwo_payload(payload, "unrelated", "lowest").offers) == 0
    rejected = parse_sonkwo_payload({"success": True, "data": {"list": [
        sku(9, supportCashPay=False)]}}, "", "lowest")
    assert not rejected.offers and rejected.warnings


def test_official_alias_cannot_hide_edition_conflict():
    from autoscout.domain import Offer

    offer = Offer("游戏", "https://www.sonkwo.hk/sku/1", Decimal("10"),
                  ("Game Deluxe Edition",))
    matched, reason = TitleCatalog({"Game": ["游戏"]}).compare_offer(offer, "Game")
    assert not matched
    assert "版本" in reason
