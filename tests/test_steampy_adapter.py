"""Regression for SteamPy's stale cards while a new search is pending."""

import pytest

from autoscout.browser import SteamPyQuotes, parse_steampy_search_payload
from autoscout.ports import SourceError
from autoscout.titles import TitleCatalog


SEARCH_RESPONSE = {
    "success": True,
    "result": {"content": [
        {"id": "101", "gameName": "Biped 2 Deluxe Edition", "gameNameCn": None},
        {"id": "102", "gameName": "Biped", "gameNameCn": "只只大冒险"},
    ]},
}


def test_search_response_uses_displayed_chinese_name_and_rejects_bad_payload():
    assert parse_steampy_search_payload(SEARCH_RESPONSE) == ("Biped 2 Deluxe Edition", "只只大冒险")
    with pytest.raises(SourceError, match="格式已改变"):
        parse_steampy_search_payload({"success": True, "result": {}})


@pytest.mark.asyncio
async def test_market_search_waits_for_current_cards_instead_of_stale_cards(tmp_path):
    from autoscout.playwright_runtime import playwright_session

    html = """
    <input class="ivu-input">
    <div id="results"><div class="gameblock"><span class="gameName">旧商品</span></div></div>
    <script>
      document.querySelector('input').addEventListener('keydown', async event => {
        if (event.key !== 'Enter') return;
        const response = await fetch('/xboot/steamGame/keyByName?gameName='
          + encodeURIComponent(event.target.value));
        const data = await response.json();
        setTimeout(() => {
          document.querySelector('#results').innerHTML = data.result.content.map(item =>
            '<div class="gameblock"><span class="gameName">'
            + (item.gameNameCn || item.gameName).slice(0, 15) + '</span></div>').join('');
        }, 300);
      });
    </script>
    """
    async with playwright_session(tmp_path) as playwright:
        try:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
        except Exception as exc:
            pytest.skip(f"Chrome not available: {exc}")
        try:
            page = await browser.new_page()
            await page.route("https://steampy.com/cdKey/cdKey",
                             lambda route: route.fulfill(body=html, content_type="text/html"))
            await page.route("https://steampy.com/xboot/steamGame/keyByName?*",
                             lambda route: route.fulfill(json=SEARCH_RESPONSE))
            await page.goto("https://steampy.com/cdKey/cdKey")
            quotes = SteamPyQuotes(page, 5000, TitleCatalog())
            names = await quotes._search_market("只只大冒险")
            assert names == ("Biped 2 Deluxe Edition", "只只大冒险")
            assert [name.strip() for name in
                    await page.locator(".gameblock .gameName").all_text_contents()] == [
                        "Biped 2 Deluxe", "只只大冒险"]
        finally:
            await browser.close()
