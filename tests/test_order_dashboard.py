"""Browser regression: the dashboard must expose all orders and their cash columns."""

from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from datetime import datetime, timezone

from autoscout.turnover import build_turnover_report, public_report
from autoscout.titles import TitleCatalog

import pytest


@pytest.mark.asyncio
async def test_order_ledger_shows_all_pages_and_separate_cash_flows(tmp_path):
    from playwright.async_api import expect
    from autoscout.playwright_runtime import playwright_session

    root = Path(__file__).parents[1] / "autoscout" / "static"
    orders = [
        {"platform": "sonkwo", "order_id": "buy-1", "title": "游戏甲",
         "occurred_at": "2026-09-30T12:00:00+00:00", "status": "completed",
         "quantity": 2, "amount": "30.00", "fee": None, "net_amount": None},
        {"platform": "steampy", "order_id": "sale-1", "title": "游戏甲",
         "occurred_at": "2026-09-30 21:00:00", "status": "sold",
         "quantity": 1, "amount": "25.00", "fee": "0.75", "net_amount": "24.25",
         "wallet_checked": True, "wallet_credit_amount": "24.25",
         "wallet_credit_matches_net": True},
        {"platform": "steampy", "order_id": "pending-1", "title": "游戏甲",
         "occurred_at": "2026-09-30 20:00:00", "status": "status_40",
         "quantity": 1, "amount": None, "fee": None, "net_amount": None,
         "wallet_checked": True, "wallet_credit_amount": None,
         "later_same_product_sale_count": 1,
         "later_same_product_sales": [{"order_id": "sale-1",
                                       "occurred_at": "2026-09-30 21:00:00",
                                       "amount": "25.00", "net_amount": "24.25"}]},
    ]
    orders.extend({"platform": "sonkwo", "order_id": f"buy-{n}", "title": f"商品{n}",
                   "occurred_at": "2026-09-29T12:00:00+00:00", "status": "completed",
                   "quantity": 1, "amount": "1.00", "fee": None, "net_amount": None}
                  for n in range(2, 59))
    orders[3] = {
        "platform": "sonkwo", "order_id": "16256369", "title": "异形工厂 等 2 件",
        "occurred_at": "2026-02-18T12:24:21+00:00", "status": "completed",
        "quantity": 2, "amount": "20.25", "fee": None, "net_amount": None,
        "purchase_display_status": "partially_refunded", "purchase_net_spent": "3.19",
        "purchase_refund_amount": "17.06",
        "purchase_lines": [
            {"line_no": 1, "title": "异形工厂", "quantity": 1, "unit_cost": "3.19",
             "refunded_quantity": 0, "refund_amount": "0.00"},
            {"line_no": 2, "title": "圣杯誓约", "quantity": 1, "unit_cost": "17.06",
             "refunded_quantity": 1, "refund_amount": "17.06"},
        ],
    }
    assert len(orders) == 60
    base_unit = {"purchase_order_id": "buy-1", "purchase_time": "2026-09-30T12:00:00+00:00",
                 "line_no": 1, "unit_no": 1, "unit_cost": "15.00", "status": "sold",
                 "purchase_quantity": 2, "purchase_unit_no": 1,
                 "sale_order_id": "sale-1", "sale_title": "游戏甲", "sale_time": "2026-09-30 21:00:00",
                 "sale_gross": "25.00", "sale_fee": "0.75", "sale_net": "24.25",
                 "order_stage_spread": "9.25", "allocation": "candidate",
                 "reason": "名称和版本一致；单一购买来源计入成本", "wallet_credit_matches_net": True}
    unmatched_unit = {**base_unit, "unit_no": 2, "purchase_unit_no": 2, "status": "unmatched",
                      "sale_order_id": None, "sale_title": None, "sale_time": None,
                      "sale_gross": None, "sale_fee": None, "sale_net": None,
                      "order_stage_spread": None, "allocation": None, "reason": None,
                      "wallet_credit_matches_net": None}
    inventory_units = [{"title": "游戏甲", **base_unit}, {"title": "游戏甲", **unmatched_unit}]
    inventory_units.extend({
        "title": f"商品{n}", **unmatched_unit,
        "purchase_order_id": f"buy-{n}",
        "purchase_time": "2026-09-29T12:00:00+00:00", "unit_cost": "1.00",
        "unit_no": 1, "purchase_quantity": 1, "purchase_unit_no": 1,
    } for n in range(2, 59))
    inventory_units[2] = {
        **unmatched_unit, "title": "Victoria 3: Dawn of Wonder",
        "purchase_order_id": "16448944", "purchase_time": "2026-03-14T15:45:16+08:00",
        "line_no": 2, "unit_no": 1, "purchase_quantity": 1, "purchase_unit_no": 1,
        "unit_cost": "16.47",
    }
    receipt = None

    async def respond(route):
        nonlocal receipt
        url = urlsplit(route.request.url)
        path = url.path
        if path == "/":
            await route.fulfill(body=(root / "index.html").read_text(encoding="utf-8"),
                                content_type="text/html")
        elif path in {"/app.js", "/style.css"}:
            await route.fulfill(body=(root / path.removeprefix("/")).read_text(encoding="utf-8"),
                                content_type="text/javascript" if path.endswith(".js") else "text/css")
        elif path == "/api/account-orders/ledger":
            query = parse_qs(url.query)
            rows = orders
            platform = query.get("platform", ["all"])[0]
            state = query.get("state", ["all"])[0]
            term = query.get("query", [""])[0].casefold()
            if term:
                rows = [row for row in rows if term in row["title"].casefold()
                        or term in row["order_id"].casefold()
                        or any(term in line["title"].casefold()
                               for line in row.get("purchase_lines", []))]
            if platform != "all":
                rows = [row for row in rows if row["platform"] == platform]
            if state == "not_counted":
                rows = [row for row in rows if row["platform"] == "steampy" and row["status"] != "sold"]
            elif state == "sold":
                rows = [row for row in rows if row["status"] == "sold"]
            elif state == "completed_buy":
                rows = [row for row in rows if row["platform"] == "sonkwo" and row["status"] == "completed"]
            page = int(query.get("page", ["1"])[0])
            size = int(query.get("page_size", ["50"])[0])
            await route.fulfill(json={"page": page, "page_size": size, "total": len(rows),
                                      "pages": max(1, (len(rows) + size - 1) // size),
                                      "rows": rows[(page - 1) * size:page * size]})
        elif path == "/api/account-orders/inventory":
            query = parse_qs(url.query)
            state = query.get("state", ["all"])[0]
            rows = inventory_units
            term = query.get("query", [""])[0].casefold()
            if term:
                rows = [row for row in rows if term in row["title"].casefold()
                        or term in row["purchase_order_id"].casefold()]
            if state == "unsold":
                rows = [row for row in rows if row["status"] == "unmatched"]
            elif state == "sold":
                rows = [row for row in rows if row["status"] == "sold"]
            elif state == "loss":
                rows = [row for row in rows if row["order_stage_spread"] is not None
                        and float(row["order_stage_spread"]) < 0]
            page = int(query.get("page", ["1"])[0])
            size = int(query.get("page_size", ["30"])[0])
            await route.fulfill(json={"ready": True, "report_start": "2025-01-01",
                                      "page": page, "page_size": size,
                                      "total": len(rows),
                                      "pages": max(1, (len(rows) + size - 1) // size),
                                      "summary": {"product_count": 58, "bought_units": 59,
                                                  "sold_units": 1, "unsold_units": 58,
                                                  "loss_units": 0, "unallocated_sales": 0,
                                                  "gross_unknown_sales": 0,
                                                  "unsold_cost": "72.00",
                                                  "order_stage_spread": "9.25"},
                                      "rows": rows[(page - 1) * size:page * size]})
        elif path == "/api/payouts/bill-1/receipt":
            receipt = route.request.post_data_json
            await route.fulfill(json={"ok": True})
        else:
            responses = {
                "/api/session": {"token": "test-token"},
                "/api/config": {"max_pages": 2, "undercut": "0.01", "sell_fee_rate": "0.03",
                                "payout_fee_rate": "0"},
                "/api/status": {"status": "failed", "stage": "finished", "discovered": 0,
                                "processed": 0, "opportunities": 0, "elapsed_seconds": None,
                                 "error": "杉果账号未登录；需要重新登录", "last_warning": None},
                "/api/cruise": {"enabled": False, "interval_seconds": 1800,
                                "next_run_at": None, "current_keyword": None, "last_error": None},
                "/api/statistics": {"run_counts": {}, "unique_offers": 0, "latest_verdicts": {},
                                    "latest_estimated_profit": "0.00", "daily": []},
                "/api/account-orders/status": {"status": "idle", "stage": "idle", "error": None},
                "/api/account-orders/summary": {"last_synced_at": "2026-09-30T12:00:00+00:00",
                                                "source_coverage": ["ordinary", "request"],
                                                "wallet_synced_at": "2026-09-30T12:00:00+00:00",
                                                "unlinked_wallet_credits": {
                                                    "count": 2, "amount": "15.28", "types": {"AK": 2}},
                                                "sonkwo": {"completed": 58, "units": 59, "spent": "87.00"},
                                                "steampy": {"sold": 1, "ordinary_sold": 1,
                                                            "request_sold": 0, "other": 1, "gross": "25.00",
                                                            "fees": "0.75", "sale_net": "24.25",
                                                            "other_statuses": {"status_40": 1}},
                                                "profit_note": "尚未配对"},
                "/api/account-orders/reconciliation": {"ready": True,
                    "completed_at": "2026-09-30T12:00:00+00:00",
                    "counts": {"candidate": 1, "fifo": 1, "review": 0, "unmatched": 0},
                    "matched_count": 2, "candidate_order_spread": "9.25",
                    "fifo_order_spread": "9.37", "matched_order_spread": "18.62", "rows": [
                        {"sale_order_id": "sale-1", "sale_title": "游戏甲",
                         "sale_time": "2026-09-30 21:00:00", "sale_gross": "25.00",
                         "sale_fee": "0.75", "sale_net": "24.25",
                         "status": "candidate", "reason": "名称、版本、数量和时间支持此购买来源",
                         "buy_order_id": "buy-1", "buy_title": "游戏甲",
                         "buy_line_no": 1, "buy_unit_no": 1,
                         "buy_unit_cost": "15.00", "order_stage_spread": "9.25"},
                        {"sale_order_id": "sale-2", "sale_title": "游戏乙",
                         "sale_time": "2026-09-30 21:10:00", "sale_gross": "21.00",
                         "sale_fee": "0.63", "sale_net": "20.37",
                         "status": "fifo", "reason": "历史订单按先购先销配对购买来源和成本",
                         "buy_order_id": "buy-2", "buy_title": "游戏乙",
                         "buy_line_no": 1, "buy_unit_no": 1,
                         "buy_unit_cost": "11.00", "order_stage_spread": "9.37"}]},
                "/api/payouts/status": {"status": "idle", "stage": "idle", "error": None},
                "/api/finance/overview": {
                    "orders_ready": True, "payouts_ready": True, "report_start": "2025-01-01",
                    "purchase_paid": "104.06", "purchase_refunds": "17.06",
                    "purchase_spent": "87.00", "purchase_units": 59, "refunded_units": 1,
                    "sale_net": "24.25", "sale_count": 1,
                    "withdrawal_debits": "301.00", "withdrawal_fees": "3.01",
                    "withdrawal_count": 1, "bank_confirmed_count": int(receipt is not None),
                    "bank_pending_count": int(receipt is None),
                    "bank_confirmed": receipt["amount"] if receipt else None,
                    "refunds_pending_platform_verification": 0,
                    "source_warnings": [],
                    "wallet": {"ready": True, "sale_credits": "24.25", "withdrawals": "301.00",
                               "withdrawal_fees": "3.01", "other_debits": "69.51", "other_credits": "349.85",
                               "period_net": "0.58", "pre_period_net": "0.00", "history_net": "0.58",
                               "available_balance": "0.58", "pending_balance": "0.00",
                               "balance_at": "2026-10-02T02:30:50+00:00", "balance_difference": "0.00",
                               "sale_credit_difference": "0.00", "sale_credits_verified": False,
                               "other_movements": [
                                   {"bill_id": "debit", "occurred_at": "2026-02-19 20:36:41", "label": "CDKey",
                                    "amount": "-69.51", "direction": "debit", "tx_id": "related-order"},
                                   {"bill_id": "credit", "occurred_at": "2026-02-18 20:00:00", "label": "平台类型 Deposit",
                                    "amount": "349.85", "direction": "credit", "tx_id": "deposit-order"},
                               ]},
                },
                "/api/account-orders/refunds": [{
                    "order_id": "16256369", "line_no": 2, "title": "圣杯誓约",
                    "purchase_time": "2026-02-18T12:24:21+00:00", "quantity": 1,
                    "refunded_quantity": 1, "refund_amount": "17.06", "refund_source": "platform",
                }],
                "/api/payouts": {"ready": True,
                    "last_synced_at": "2026-09-30T12:00:00+00:00",
                    "wallet_bill_count": 2, "withdrawal_count": 1,
                    "wallet_debits": "301.00", "wallet_fees": "3.01",
                    "bank_confirmed": receipt["amount"] if receipt else "0.00",
                    "awaiting_bank_check": int(receipt is None),
                    "unlinked_fee_count": 0, "rows": [{
                        "bill_id": "bill-1", "occurred_at": "2026-08-30 13:35:27",
                        "wallet_debit": "301.00", "wallet_fee": "3.01",
                        "bank_received": receipt["amount"] if receipt else None,
                        "bank_received_at": receipt["received_at"] if receipt else None,
                        "bank_note": None, "bank_difference": None,
                        "status": "manual_receipt" if receipt else "bank_unverified"}]},
                "/api/assessments": [], "/api/trades": [],
                "/api/cash": {"realized_profit": "0.00", "capital_tied": "0.00",
                              "open_positions": 0, "awaiting_payout": 0, "cash_received": "0.00"},
            }
            responses['/api/account-orders/turnover'] = public_report(build_turnover_report({
                'ready':True,'rows':inventory_units,'report_start':'2025-01-01','summary':{},
                'source_coverage':['ordinary','request'],
                'source_synced_at':{key:'2026-09-30T12:00:00+00:00' for key in ('sonkwo','ordinary','request')}},
                TitleCatalog(),now=datetime(2026,10,2,tzinfo=timezone.utc)))
            assert path in responses, path
            await route.fulfill(json=responses[path])

    async with playwright_session(tmp_path) as playwright:
        try:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
        except Exception as exc:
            pytest.skip(f"Chrome not available: {exc}")
        try:
            page = await browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.route("http://testserver/**", respond)
            await page.goto("http://testserver/")
            await expect(page.locator("#finance-purchase-spent")).to_have_text("¥87.00")
            await expect(page.locator("#finance-purchase-details")).to_contain_text("¥104.06")
            await expect(page.locator("#finance-purchase-details")).to_contain_text("¥17.06")
            await expect(page.locator("#finance-sale-net")).to_have_text("¥24.25")
            await expect(page.locator("#finance-withdrawals")).to_have_text("¥301.00")
            await expect(page.locator("#finance-withdrawal-fees")).to_have_text("¥3.01")
            await expect(page.locator("#finance-bank-received")).to_have_text("待核对")
            await expect(page.locator("#finance-wallet-other-debits")).to_have_text("¥69.51")
            await expect(page.locator("#finance-wallet-balance")).to_have_text("¥0.58")
            await expect(page.locator("#finance-wallet-gap")).to_have_text("¥0.00")
            await expect(page.locator("#wallet-equation")).to_contain_text("其他入账 ¥349.85")
            await expect(page.locator("#wallet-reconciliation-note")).to_contain_text("尚未逐笔全部核对")
            await page.locator("#wallet-other-details summary").click()
            await expect(page.locator("#wallet-other-rows > tr")).to_have_count(2)
            await expect(page.locator("#wallet-other-rows > tr").first).to_contain_text("CDKey")
            await expect(page.locator("#wallet-other-rows > tr").first).to_contain_text("¥69.51")
            await expect(page.locator("#wallet-other-rows > tr").nth(1)).to_contain_text("¥349.85")
            await expect(page.locator("#refund-count")).to_have_text("(1 件)")
            await expect(page.locator("#refund-rows")).to_contain_text("平台退款成功记录")
            await expect(page.locator("#scan-alert")).to_contain_text("杉果账号未登录")
            await expect(page.locator("#inventory-count")).to_have_text("符合筛选 59 件购买商品")
            await expect(page.locator("#inventory-bought")).to_have_text("59")
            await expect(page.locator("#inventory-unsold")).to_have_text("58")
            await expect(page.locator("#inventory-rows > tr")).to_have_count(30)
            assert "sale-1" in await page.locator("#inventory-rows > tr").nth(0).inner_text()
            assert "未找到对应成交" in await page.locator("#inventory-rows > tr").nth(1).inner_text()
            assert "同单买入 2 件 · 本行第 2 件" in await page.locator("#inventory-rows > tr").nth(1).inner_text()
            await expect(page.locator("#account-coverage")).to_contain_text("2 条正向钱包流水")
            await expect(page.locator("#account-coverage")).to_contain_text("已纳入 SteamPy 普通与求购订单")
            assert "buy-1" in await page.locator("#inventory-rows > tr").nth(0).inner_text()
            assert "buy-1" in await page.locator("#inventory-rows > tr").nth(1).inner_text()
            await page.locator("#inventory-next").click()
            await expect(page.locator("#inventory-page")).to_have_text("第 2 / 2 页")
            await expect(page.locator("#inventory-rows > tr")).to_have_count(29)
            await page.locator("#inventory-state").select_option("sold")
            await expect(page.locator("#inventory-count")).to_have_text("符合筛选 1 件购买商品")
            await page.locator("#inventory-state").select_option("all")
            await page.locator("#inventory-query").fill("Victoria 3: Dawn of Wonder")
            await page.locator("#inventory-form button[type=submit]").click()
            await expect(page.locator("#inventory-count")).to_have_text("符合筛选 1 件购买商品")
            await expect(page.locator("#inventory-rows > tr")).to_have_count(1)
            await expect(page.locator("#inventory-rows")).to_contain_text("订单 16448944 · 买入 1 件")
            await expect(page.locator("#inventory-rows")).to_contain_text("未找到对应成交")
            assert "第 2 项" not in await page.locator("#inventory-rows").inner_text()
            await expect(page.locator("#unconfirmed-count")).to_have_text("共 1 条记录")
            assert "pending-1" in await page.locator("#unconfirmed-rows").inner_text()
            await expect(page.locator("#ledger-count")).to_have_text("符合条件 60 单")
            await expect(page.locator("#account-ledger-rows tr")).to_have_count(50)
            assert "¥30.00" in await page.locator("#account-ledger-rows").inner_text()
            assert "¥25.00" in await page.locator("#account-ledger-rows").inner_text()
            assert "¥0.75" in await page.locator("#account-ledger-rows").inner_text()
            assert "平台码 40" in await page.locator("#account-ledger-rows").inner_text()
            assert "同款之后有 1 笔成功成交" in await page.locator("#account-ledger-rows").inner_text()
            assert "钱包同订单号入账金额匹配" in await page.locator("#account-ledger-rows").inner_text()
            await page.locator("#sale-match-details summary").click()
            await expect(page.locator("#match-rows tr")).to_have_count(2)
            assert "¥9.25" in await page.locator("#match-rows").inner_text()
            await expect(page.locator("#match-fifo")).to_have_text("1")
            await expect(page.locator("#match-spread")).to_have_text("¥18.62")
            await page.locator("#match-state").select_option("fifo")
            await expect(page.locator("#match-rows tr")).to_have_count(1)
            assert "sale-2" in await page.locator("#match-rows").inner_text()
            assert "¥11.00" in await page.locator("#match-rows").inner_text()
            assert "¥301.00" in await page.locator("#payout-rows").inner_text()
            assert "银行到账待核对" in await page.locator("#payout-rows").inner_text()
            await page.locator("#ledger-next").click()
            await expect(page.locator("#account-ledger-rows tr")).to_have_count(10)
            await page.locator("#account-state").select_option("not_counted")
            await expect(page.locator("#ledger-count")).to_have_text("符合条件 1 单")
            await expect(page.locator("#account-ledger-rows tr")).to_have_count(1)
            assert "已同步钱包流水中未见此订单号入账" in await page.locator("#account-ledger-rows").inner_text()
            assert "sale-1" in await page.locator("#account-ledger-rows").inner_text()
            await page.locator("#account-state").select_option("all")
            await page.locator("#account-query").fill("圣杯誓约")
            await page.locator("#account-ledger-form button[type=submit]").click()
            await expect(page.locator("#ledger-count")).to_have_text("符合条件 1 单")
            await expect(page.locator("#account-ledger-rows")).to_contain_text("16256369")
            await expect(page.locator("#account-ledger-rows")).to_contain_text("圣杯誓约 × 1 件 · 单件 ¥17.06")
            await expect(page.locator("#account-ledger-rows")).to_contain_text("异形工厂 × 1 件 · 单件 ¥3.19")
            await expect(page.locator("#account-ledger-rows")).to_contain_text("部分商品已退款")
            await expect(page.locator("#account-ledger-rows")).to_contain_text("已退款 1 件")
            await page.locator("#payout-rows button").first.click()
            await page.locator("#bank-amount").fill("301.00")
            await page.locator("#bank-date").fill("2026-08-31")
            await page.locator("#bank-form button[type=submit]").click()
            await expect(page.locator("#finance-bank-received")).to_have_text("¥301.00")
            await expect(page.locator("#finance-bank-details")).to_contain_text("仍有 0 笔待核对")
            assert not errors, errors
        finally:
            await browser.close()
