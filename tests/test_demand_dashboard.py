"""The browser UI must separate demand, asking profit and unknown sales."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from playwright.async_api import expect
import pytest

from autoscout.demand import market_trend
from autoscout.playwright_runtime import playwright_session
from autoscout.repository import Repository
from autoscout.settings import Settings
from autoscout.web import create_app
from test_demand import appraisal, snapshot


@pytest.mark.asyncio
async def test_price_spreads_are_separate_and_trend_dialog_preserves_unknown_sales(tmp_path):
    repo = Repository(tmp_path / 'dashboard.sqlite3')
    repo.begin_run('demand', '')
    now = datetime.now(timezone.utc)
    current = snapshot(open_requests=1, best_request_price='11.00')
    prior = snapshot(observed_at=(now-timedelta(days=1)).isoformat(timespec='seconds'), lowest_ask='120.00', stock=200, open_requests=5, best_request_price='12.00')
    current['trend'] = market_trend(current, [prior])
    cases = [current, snapshot(), snapshot(open_requests=1, best_request_price='9.00'),
             snapshot(open_requests=None, requests_complete=False, request_issue='求购读取超时')]
    identifiers = []
    for index, demand in enumerate(cases, 1):
        result = appraisal(demand)
        result = replace(result, offer=replace(result.offer, url=f'https://www.sonkwo.hk/sku/{index}'))
        identifiers.append(repo.save_assessment('demand', result))
    repo.update_run('demand','completed','finished',4,4,1,0,finished=True)
    with TestClient(create_app(Settings(tmp_path),repo)) as client:
        async def respond(route):
            response = await asyncio.to_thread(client.get, route.request.url)
            await route.fulfill(status=response.status_code,body=response.content,
                                content_type=response.headers.get('content-type'))
        async with playwright_session(tmp_path) as playwright:
            browser = await playwright.chromium.launch(channel='chrome',headless=True)
            try:
                page = await browser.new_page()
                errors = []
                page.on('pageerror',lambda error:errors.append(str(error)))
                await page.route('http://testserver/**',respond)
                await page.goto('http://testserver/')
                await expect(page.locator('#results tr')).to_have_count(1)
                await expect(page.locator('#latest-estimate')).to_have_text('¥0.56')
                await expect(page.locator('#results')).to_contain_text('按求购利润 ¥0.56 / 5.60%')
                await page.get_by_role('button',name='查看市场趋势',exact=True).click()
                await expect(page.locator('#market-trend-dialog')).to_be_visible()
                await expect(page.locator('#market-trend-chart svg')).to_have_count(1)
                await expect(page.locator('#market-trend-rows tr')).to_have_count(2)
                await expect(page.locator('#market-sales-evidence')).to_contain_text('近 7 天销量：未知')
                await expect(page.locator('#market-trend-summary')).to_contain_text('挂价 -16.67%')
                await page.locator('#market-trend-close').click()
                await page.locator('#result-filter').select_option('price_only')
                await expect(page.locator('#results tr')).to_have_count(3)
                await expect(page.locator(f'#results tr[data-assessment-id="{identifiers[1]}"]')).to_contain_text('当前无公开求购')
                await expect(page.locator(f'#results tr[data-assessment-id="{identifiers[2]}"]')).to_contain_text('按求购利润 ¥-1.36 / -13.60%')
                await expect(page.locator(f'#results tr[data-assessment-id="{identifiers[3]}"]')).to_contain_text('需求尚未核实')
                assert errors == []
            finally:
                await browser.close()
