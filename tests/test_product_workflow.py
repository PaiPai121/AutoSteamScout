"""Exercise the whole read-only discovery and web confirmation workflows."""

import asyncio
from dataclasses import replace
from decimal import Decimal
import json

import pytest
from fastapi.testclient import TestClient
from playwright.async_api import expect

from autoscout.browser import SteamPyQuotes
from autoscout.domain import MarketLookup, Offer, PricingPolicy, Verdict, assess
from autoscout.playwright_runtime import playwright_session
from autoscout.products import ProductIdentity
from autoscout.repository import Repository
from autoscout.settings import Settings
from autoscout.titles import TitleCatalog
from autoscout.web import create_app


def source_offer():
    product = ProductIdentity("sonkwo", "123", ("中文甲", "Long Official Name"),
                              "https://www.sonkwo.hk/sku/123", "base", "cn", detail_checked=True)
    return Offer(product.names[0], product.url, Decimal("10"), product.names[1:], product)


MARKET_ITEM = {"id": "987", "gameNameCn": "中文乙", "gameName": "Short Name",
               "appId": "42", "gameUrl": "https://store.steampowered.com/app/42/",
               "steamApp": {"appId": "42", "type": "game"}}


@pytest.mark.asyncio
async def test_capture_confirm_then_reuse_pair_and_check_detail_before_profit(tmp_path, monkeypatch):
    repo = Repository(tmp_path / "test.sqlite3")
    left = source_offer()
    market_html = """<input class="ivu-input"><div id="cards"></div><script>
      document.querySelector('input').addEventListener('keydown', async event => {
        if (event.key !== 'Enter') return;
        const data = await (await fetch('/xboot/steamGame/keyByName?gameName=' + encodeURIComponent(event.target.value))).json();
        setTimeout(() => document.querySelector('#cards').innerHTML = data.result.content.map(item =>
          '<div class="gameblock"><span class="gameName">' + (item.gameNameCn || item.gameName).slice(0,15) + '</span></div>').join(''), 75);
      });</script>"""
    detail_html = """<div id="quote"></div><script>
      fetch('/xboot/steamGame/getOne?id=987').then(response => response.json()).then(data => {
        document.querySelector('#quote').innerHTML = '<table><tbody class="ivu-table-tbody"><tr class="ivu-table-row">'
          + '<td>seller</td><td></td><td></td><td>2</td><td>¥20.00</td></tr></tbody></table>';
      });
      fetch('/xboot/steamKeySale/listSale?gameId=987&pageNumber=1&pageSize=20&sort=keyPrice&order=asc');
      </script>"""
    async with playwright_session(tmp_path) as playwright:
        browser = await playwright.chromium.launch(channel="chrome", headless=True)
        try:
            page = await browser.new_page()
            items = [dict(MARKET_ITEM)]
            await page.route("https://steampy.com/market", lambda route: route.fulfill(body=market_html, content_type="text/html"))
            await page.route("https://steampy.com/xboot/steamGame/keyByName?*", lambda route: route.fulfill(
                json={"success": True, "result": {"content": items, "totalPages": 1}}))
            await page.route("https://steampy.com/cdkDetail?*", lambda route: route.fulfill(body=detail_html, content_type="text/html"))
            await page.route("https://steampy.com/xboot/steamGame/getOne?*", lambda route: route.fulfill(
                json={"success": True, "result": MARKET_ITEM}))
            await page.route("https://steampy.com/xboot/steamKeySale/listSale?*", lambda route: route.fulfill(json={
                "success": True, "result": {"content": [{"saleId": "s1", "ccy": "CNY", "stock": 2, "keyPrice": 20}],
                "totalElements": 1, "totalPages": 1, "number": 0}}))
            # APIRequestContext bypasses page routes; provide this public endpoint
            # at the transport boundary while running the actual demand collector.
            from playwright.async_api import APIRequestContext
            class RequestResponse:
                ok = True
                async def json(self):
                    from datetime import datetime, timezone
                    return {"success": True, "result": {"content": [{"id": "w1", "gameId": "987",
                            "gameName": "Short Name", "gameNameCn": "中文乙", "txStatus": "02", "delFlag": 0,
                            "txPrice": 19, "createTime": datetime.now(timezone.utc).isoformat()}],
                            "totalElements": 1, "totalPages": 1, "number": 0}}
            async def read_request(self, url, **kwargs):
                assert url == 'https://steampy.com/xboot/wantKeyOrder/showGame'
                assert kwargs['params']['gameId'] == '987'
                return RequestResponse()
            monkeypatch.setattr(APIRequestContext, 'get', read_request)
            quotes = SteamPyQuotes(page, 5000, TitleCatalog(), repo.product_mapping)
            async def open_market(): await page.goto("https://steampy.com/market")
            quotes._open_market = open_market
            lookup = await quotes.lookup(left)
            assert lookup.quote is None and lookup.products[0].alternate_titles == ("Short Name",)
            repo.begin_run("first", "")
            saved = repo.save_assessment("first", assess(left, lookup, TitleCatalog(), PricingPolicy()))
            repo.confirm_product_mapping(saved, "987", "same variant and content")
            lookup = await quotes.lookup(left)
            assert lookup.quote.product.product_id == "987" and lookup.quote.product.detail_checked
            result = assess(left, lookup, TitleCatalog(), PricingPolicy(), repo.product_mapping("123"))
            assert result.verdict == Verdict.OPPORTUNITY and result.matching["method"] == "confirmed_product_pair"
            assert result.net_profit == Decimal("9.20")
            assert result.liquidity['request_profit'] == '8.25'
            # A stale detail for another product cannot supply this quote.
            await page.unroute("https://steampy.com/xboot/steamGame/getOne?*")
            await page.route("https://steampy.com/xboot/steamGame/getOne?*", lambda route: route.fulfill(
                json={"success": True, "result": {**MARKET_ITEM, "id": "999"}}))
            with pytest.raises(Exception, match="详情编号"):
                await quotes.lookup(left)
            # Distinct IDs with the same complete names require disambiguation.
            repo.clear_product_mapping("123")
            items[:] = [{**MARKET_ITEM, "gameName": "Long Official Name"},
                        {**MARKET_ITEM, "id": "988", "gameName": "Long Official Name"}]
            ambiguous = await quotes.lookup(left)
            assert ambiguous.quote is None and "多个" in ambiguous.issue
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_web_review_dialog_saves_and_revokes_only_captured_pair(tmp_path):
    from autoscout.browser import parse_steampy_product
    repo = Repository(tmp_path / "test.sqlite3")
    left, right = source_offer(), parse_steampy_product(MARKET_ITEM)
    repo.begin_run("web", "")
    saved = repo.save_assessment("web", assess(left, MarketLookup(candidates=(right.title,), products=(right,)), TitleCatalog(), PricingPolicy()))
    repo.update_run("web", "completed", "finished", 1, 1, 0, 0, finished=True)
    app = create_app(Settings(tmp_path), repo)
    with TestClient(app) as client:
        async def respond(route):
            request = route.request
            headers = {key: value for key, value in request.headers.items() if key.lower() == 'x-scout-token'}
            response = await asyncio.to_thread(client.request, request.method, request.url,
                                               content=request.post_data, headers=headers)
            await route.fulfill(status=response.status_code, body=response.content,
                                content_type=response.headers.get('content-type'))
        async with playwright_session(tmp_path) as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            try:
                page = await browser.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                await page.route('http://testserver/**', respond)
                await page.goto('http://testserver/')
                await page.locator('#result-filter').select_option('needs_review')
                await expect(page.locator('#results tr')).to_have_count(1)
                await page.locator('#results .product-evidence summary').click()
                await expect(page.locator('#product-evidence-dialog')).to_be_visible()
                await expect(page.locator('#product-evidence-offer')).to_contain_text('Long Official Name')
                await page.locator('#product-evidence-close').click()
                await page.get_by_role('button', name='核对对应商品', exact=True).click()
                await expect(page.locator('#product-offer')).to_contain_text('Long Official Name')
                await expect(page.locator('#product-market')).to_contain_text('Short Name')
                await expect(page.locator('#product-market')).to_contain_text('Steam AppID：42')
                await page.locator('#product-confirmed').check()
                await page.locator('#product-note').fill('verified matching content')
                await page.locator('#product-save').click()
                await expect(page.locator('#product-dialog')).not_to_be_visible()
                assert repo.product_mapping('123')['steampy_id'] == '987'
                await page.locator('#product-mapping-list summary').click()
                await expect(page.locator('#product-mapping-rows')).to_contain_text('verified matching content')
                await page.get_by_role('button', name='撤销对应关系', exact=True).click()
                await expect(page.locator('#product-mapping-rows')).to_contain_text('暂无确认关系')
                assert repo.product_mapping('123') is None and errors == []
            finally:
                await browser.close()
