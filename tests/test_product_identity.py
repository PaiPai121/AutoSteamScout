"""General regressions for multilingual matching and scoped confirmations."""

from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from autoscout.browser import parse_sonkwo_product_page, parse_steampy_products
from autoscout.domain import MarketLookup, MarketQuote, Offer, PricingPolicy, Verdict, assess
from autoscout.ports import SourceError
from autoscout.products import MarketProduct, ProductIdentity, compare_products
from autoscout.repository import Repository
from autoscout.settings import Settings
from autoscout.titles import TitleCatalog
from autoscout.web import create_app


def offer(title="中文甲", alternate="Official Game", **fields):
    names = (title, alternate)
    product = ProductIdentity("sonkwo", "123", names, "https://www.sonkwo.hk/sku/123",
                              "base", "cn", detail_checked=True, **fields)
    return Offer(title, product.url, Decimal("10"), (alternate,), product)


def market(title="中文乙", alternate="Official Game", product_id="987", **fields):
    names = (title, alternate)
    product = ProductIdentity("steampy", product_id, names, f"https://steampy.com/cdkDetail?name=cn&gameId={product_id}",
                              "base", "cn", **fields)
    return MarketProduct(title, (alternate,), product)


def save_review(repo, left, right, run="run"):
    repo.begin_run(run, "")
    result = assess(left, MarketLookup(candidates=(right.title,), products=(right,)), TitleCatalog(), PricingPolicy())
    saved = repo.save_assessment(run, result)
    repo.update_run(run, "completed", "finished", 1, 1, 0, 0, finished=True)
    return saved


def test_both_platform_official_names_are_used_and_recorded():
    # Distinct translations, the same official English variant name.
    products = parse_steampy_products({"success": True, "result": {"content": [{
        "id": "987", "gameNameCn": "另一种译名", "gameName": "Official Game",
        "appId": "42", "gameUrl": "https://store.steampowered.com/app/42/",
        "bundleId": "000000", "gamePath": "unverified", "steamApp": {"appId": "42", "type": "game"}
    }]}})
    right = products[0]
    assert right.alternate_titles == ("Official Game",)
    assert right.identity.steam_app_id == "42" and right.identity.steam_package_id is None
    result = assess(offer(), MarketLookup(MarketQuote(right.title, (Decimal("20"),), right.identity.url,
                                                    right.alternate_titles, replace(right.identity, detail_checked=True))), TitleCatalog(), PricingPolicy())
    assert result.verdict == Verdict.PRICE_ONLY and result.matching['accepted']
    assert result.matching["method"] == "official_names"
    assert result.matching["offer_name"] == result.matching["market_name"] == "Official Game"


def test_steam_parent_name_cannot_hide_market_variant_edition():
    right = parse_steampy_products({"success": True, "result": {"content": [{
        "id": "987", "gameNameCn": "游戏 豪华版", "gameName": "Game Deluxe Edition",
        "appId": "42", "steamApp": {"appId": "42", "gameName": "Game", "type": "game"}
    }]}})[0]
    assert "Game" not in right.alternate_titles
    assert compare_products(offer("游戏", "Game"), right, TitleCatalog()).blocked


@pytest.mark.parametrize("change", ["app", "package", "edition", "type", "official_conflict", "region"])
def test_identical_or_aliased_names_cannot_override_metadata_conflicts(change):
    left, right = offer(steam_app_id="42", steam_package_id="12"), market(steam_app_id="42", steam_package_id="12")
    if change == "app": right = replace(right, identity=replace(right.identity, steam_app_id="43"))
    if change == "package": right = replace(right, identity=replace(right.identity, steam_package_id="13"))
    if change == "edition": right = market("中文乙 豪华版", "Official Game Deluxe Edition")
    if change == "type": right = replace(right, identity=replace(right.identity, content_type="dlc"))
    if change == "region": right = replace(right, identity=replace(right.identity, market_region="global"))
    if change == "official_conflict": right = market("中文乙", "Official Game Deluxe Edition")
    result = compare_products(left, right, TitleCatalog({"中文甲": ["中文乙"]}))
    assert not result.accepted and result.blocked


def test_similar_spelling_and_abbreviation_do_not_auto_match():
    assert not compare_products(offer("游戏 豪华版", "Word Rally Championship 8 Deluxe Edition"),
                                market("游戏乙 豪华版", "WRC 8 Deluxe Edition"), TitleCatalog()).accepted
    assert not compare_products(offer("游戏", "Official Game 2"), market("中文乙", "Official Game 3"), TitleCatalog()).accepted
    assert not TitleCatalog().compare("Game 1.2", "Game 12")[0]


def test_explicit_edition_phrase_can_occur_in_either_order_or_middle():
    catalog = TitleCatalog()
    assert catalog.compare("WRC 9 Edition Deluxe FIA World Rally Championship (Steam)",
                           "WRC 9 FIA World Rally Championship Deluxe Edition")[0]
    assert not catalog.compare("Gold Rush", "Rush Gold Edition")[0]
    assert not catalog.compare("Royal Quest", "Quest Royal Edition")[0]


def test_mapping_is_scoped_to_ids_and_revalidates_source_information(tmp_path):
    repo = Repository(tmp_path / "test.sqlite3")
    left, right = offer("游戏", "Long Official Name"), market("游戏乙", "Short Name")
    saved = save_review(repo, left, right)
    mapping = repo.confirm_product_mapping(saved, "987", "checked content")
    assert compare_products(left, right, TitleCatalog(), mapping).method == "confirmed_product_pair"
    changed = replace(right, identity=replace(right.identity, names=("Changed Name",)))
    assert not compare_products(left, changed, TitleCatalog(), mapping).accepted
    assert not compare_products(left, market(product_id="988"), TitleCatalog(), mapping).accepted
    assert not compare_products(offer("其他商品", "Long Official Name"), right, TitleCatalog(), mapping).accepted
    # A detail check/timestamp is not a product change, but a different DLC is.
    assert compare_products(left, replace(right, identity=replace(right.identity, detail_checked=True)), TitleCatalog(), mapping).accepted
    repo.clear_product_mapping("123")
    assert repo.product_mapping("123") is None


def test_confirmation_rejects_unknown_candidates_stale_rows_and_hard_conflicts(tmp_path):
    repo = Repository(tmp_path / "test.sqlite3")
    left, right = offer(), market()
    saved = save_review(repo, left, right)
    with pytest.raises(ValueError, match="候选"):
        repo.confirm_product_mapping(saved, "999")
    new_saved = save_review(repo, left, market("中文乙 豪华版", "Official Game Deluxe Edition"), "new")
    with pytest.raises(ValueError, match="新扫描"):
        repo.confirm_product_mapping(saved, "987")
    with pytest.raises(ValueError, match="版本不一致"):
        repo.confirm_product_mapping(new_saved, "987")
    assert repo.product_mappings() == []


def test_mapping_api_roundtrip_requires_token_and_uses_captured_products(tmp_path):
    repo = Repository(tmp_path / "test.sqlite3")
    saved = save_review(repo, offer("中文", "Long Name"), market("另外中文", "Short Name"))
    with TestClient(create_app(Settings(tmp_path), repo)) as client:
        body = {"assessment_id": saved, "steampy_id": "987", "note": "same contents"}
        assert client.post("/api/product-mappings", json=body).status_code == 403
        token = client.get("/api/session").json()["token"]
        headers = {"X-Scout-Token": token}
        assert client.post("/api/product-mappings", json=body, headers=headers).status_code == 201
        assert len(client.get("/api/product-mappings").json()) == 1
        assert client.delete("/api/product-mappings/123", headers=headers).status_code == 200
        assert client.get("/api/product-mappings").json() == []
        assert client.get("/api/assessments").json()[0]["matching"]["offer"]["product_id"] == "123"


def test_sonkwo_detail_must_keep_original_product_id_and_name():
    left = offer("澳网公开赛2", "AO Tennis 2", parent_product_id="2353")
    loader = {"current_sku": 123, "type": "Products::Base", "skus": [{
        "id": 123, "product_id": 2353, "sku_name": "澳网公开赛2", "sku_ename": "AO Tennis 2",
        "region": "cn", "key_type": "steam_key"}]}
    def html():
        return "<script>window.__remixContext = " + json.dumps({"state": {"loaderData": {
            "routes/__pc/__pages/sku/$id": loader}}}, ensure_ascii=False) + ";</script>"
    assert parse_sonkwo_product_page(left, html()).product.detail_checked
    loader["skus"][0]["product_id"] = 3446
    with pytest.raises(SourceError, match="所属产品编号"):
        parse_sonkwo_product_page(left, html())
    loader["skus"][0]["product_id"] = 2353
    loader["skus"][0]["sku_name"] = "错误商品"
    loader["skus"][0]["sku_ename"] = "Wrong Product"
    with pytest.raises(SourceError, match="名称"):
        parse_sonkwo_product_page(left, html())


def test_steampy_disagreeing_app_id_fields_fail_closed():
    with pytest.raises(SourceError, match="AppID"):
        parse_steampy_products({"success": True, "result": {"content": [{
            "id": "987", "gameName": "Game", "appId": "42",
            "gameUrl": "https://store.steampowered.com/app/43/"}]}})


def test_real_public_snapshots_preserve_all_names_that_previously_failed_matching():
    fixture = json.loads((Path(__file__).parent / "fixtures" / "public_discovery_identities.json").read_text(encoding="utf-8"))
    for row in fixture["products"]:
        left, right = ProductIdentity.from_snapshot(row["offer"]), ProductIdentity.from_snapshot(row["market"])
        source = Offer(row["offer_title"], left.url, Decimal(row["offer_cost"]), left.names[1:], left)
        target = MarketProduct(right.names[0], right.names[1:], right)
        result = compare_products(source, target, TitleCatalog())
        assert result.accepted, row["offer_title"]
        assert result.offer_name == row["expected_offer_name"]
        assert result.market_name == row["expected_market_name"]
        assert source.product.steam_app_id is None  # do not invent cross-platform AppID verification
