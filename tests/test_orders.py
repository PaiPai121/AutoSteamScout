"""Regression contract based on sanitized live account order response shapes."""

from decimal import Decimal

from fastapi.testclient import TestClient
import pytest

from autoscout.orders import (AccountOrder, OrderSyncService, PurchaseLine, parse_sonkwo_orders,
                              parse_steampy_orders, parse_steampy_request_orders)
from autoscout.payouts import WalletBill
from autoscout.ports import SourceError
from autoscout.reconcile import reconcile_orders
from autoscout.repository import Repository
from autoscout.settings import Settings
from autoscout.titles import TitleCatalog
from autoscout.web import create_app


def account_rows():
    sonkwo = [{
        "id": "buy-1", "state": "completed", "createdAt": 1790798400000,
        "subtotal": 30, "subOrders": [{"status": 1, "quantity": 2, "realPrice": 15,
                                      "sku": {"chsName": "游戏甲", "enName": "Game A"}}],
    }]
    normal = [
        {"id": "sale-1", "txStatus": 20, "gameNameCn": "游戏甲", "txTime": "2026-09-30 12:00:00",
         "cdKey": "SECRET-KEY-MUST-NOT-PERSIST"},
        {"id": "sale-2", "txStatus": 40, "gameNameCn": "游戏乙", "txTime": "2026-09-30 13:00:00"},
    ]
    successful = [{
        "id": "sale-1", "txPrice": 25, "fee": 0.70, "techFee": 0.05, "osFee": 0,
        "txTime": "2026-09-30 12:00:00", "refundTime": None,
        "steamGame": {"gameNameCn": "游戏甲"}, "cdKey": "SECRET-KEY-MUST-NOT-PERSIST",
    }]
    return sonkwo, normal, successful


def test_account_order_import_statistics_and_secret_whitelist(tmp_path):
    settings = Settings(tmp_path)
    repo = Repository(settings.database_path)
    sonkwo, normal, successful = account_rows()
    parsed = [*parse_sonkwo_orders(sonkwo), *parse_steampy_orders(normal, successful)]
    repo.replace_account_orders(parsed)
    repo.replace_account_orders(parsed)
    summary = repo.account_order_summary()
    assert summary["sonkwo"] == {"orders": 1, "completed": 1, "units": 2, "spent": "30.00",
                                 "paid": "30.00", "refund_amount": "0.00", "refunded_units": 0,
                                 "refunded_orders": 0}
    assert summary["steampy"] == {"orders": 2, "sold": 1, "ordinary_sold": 1,
                                  "request_sold": 0, "request_net": "0.00",
                                  "gross_known_sold": 1, "other": 1,
                                  "gross": "25.00", "fees": "0.75", "sale_net": "24.25",
                                  "other_statuses": {"status_40": 1}}
    assert summary["realized_profit"] is None
    assert len(repo.account_orders("steampy")) == 2
    first = repo.account_order_ledger(page_size=2)
    second = repo.account_order_ledger(page=2, page_size=2)
    assert first["total"] == 3 and first["pages"] == 2
    assert len(first["rows"]) == 2 and len(second["rows"]) == 1
    assert {row["order_id"] for row in first["rows"] + second["rows"]} == {
        "buy-1", "sale-1", "sale-2"}
    assert repo.account_order_ledger(state="not_counted")["rows"][0]["status"] == "status_40"
    assert repo.account_order_ledger(query="sale-1")["total"] == 1
    assert repo.account_order_ledger(platform="sonkwo")["total"] == 1
    assert "SECRET-KEY" not in settings.database_path.read_bytes().decode("utf-8", errors="ignore")
    with TestClient(create_app(settings, repo)) as client:
        assert client.get("/api/account-orders/summary").json()["steampy"]["sold"] == 1
        assert client.get("/api/account-orders/ledger?page_size=2").json()["total"] == 3
        assert client.get("/api/account-orders/ledger?state=not_counted").json()["total"] == 1
        assert client.get("/api/account-orders/ledger?page_size=0").status_code == 400
        assert "SECRET-KEY" not in client.get("/api/account-orders/steampy").text
        assert "SECRET-KEY" not in client.get("/api/account-orders/ledger").text
        assert client.get("/api/account-orders/unknown").status_code == 404
        assert client.post("/api/account-orders/sync").status_code == 403


def test_bad_snapshot_preserves_last_complete_account_orders(tmp_path):
    repo = Repository(Settings(tmp_path).database_path)
    sonkwo, normal, successful = account_rows()
    parsed = [*parse_sonkwo_orders(sonkwo), *parse_steampy_orders(normal, successful)]
    repo.replace_account_orders(parsed)
    first_sync = repo.account_order_summary()["last_synced_at"]
    with pytest.raises(ValueError, match="重复"):
        repo.replace_account_orders([*parsed, parsed[0]])
    assert repo.account_order_summary()["last_synced_at"] == first_sync
    assert len(repo.account_orders("sonkwo")) == 1


def test_unlinked_positive_wallet_bills_expose_order_source_gap(tmp_path):
    repo = Repository(Settings(tmp_path).database_path)
    sonkwo, normal, successful = account_rows()
    repo.replace_account_orders([*parse_sonkwo_orders(sonkwo),
                                 *parse_steampy_orders(normal, successful)])
    repo.replace_wallet_bills([
        WalletBill("ordinary-credit", "2026-09-30 12:00:00", Decimal("24.25"),
                   "K", "sale-1", "D"),
        WalletBill("request-credit-1", "2026-09-30 13:00:00", Decimal("5.58"),
                   "AK", "request-1", "D"),
        WalletBill("request-credit-2", "2026-09-30 14:00:00", Decimal("9.70"),
                   "AK", "request-2", "D"),
        WalletBill("archived-credit", "2024-12-31 23:59:59", Decimal("2.00"),
                   "AK", "old-request", "D"),
        WalletBill("unknown-debit", "2026-09-30 14:30:00", Decimal("-3.50"),
                   "w-K", "unknown-1", "C"),
    ])
    summary = repo.account_order_summary()
    assert summary["steampy"]["sold"] == 1
    assert summary["unlinked_wallet_credits"] == {
        "count": 2, "amount": "15.28", "types": {"AK": 2}}
    assert summary["wallet_synced_at"] is not None
    assert summary["unclassified_wallet_movements"] == {
        "count": 1, "debits": "3.50", "credits": "0.00", "types": {"w-K": 1}}


def test_request_sales_join_wallet_and_purchase_inventory_without_invented_fees(tmp_path):
    """Sanitized shapes from SteamPy's request-order tab and AK wallet credits."""
    repo = Repository(Settings(tmp_path).database_path)
    purchase = AccountOrder(
        "sonkwo", "purchase-1", "生化奇兵：无限", "2026-03-23T10:54:13+00:00",
        "completed", 1, Decimal("21.62"),
        lines=(PurchaseLine(
            1, "生化奇兵：无限", ("Bioshock Infinite",), 1, Decimal("21.62")),))
    request_rows = [{
        "id": "request-1", "gameNameCn": "生化奇兵：无限",
        "gameName": "Bioshock Infinite", "createTime": "2026-08-23 12:54:51",
        "updateTime": "2026-08-24 08:02:06",
        "txStatus": "20", "txPrice": "9.70", "oriPrice": 10.0,
        "cdKey": "SECRET-KEY-MUST-NOT-PERSIST",
    }, {
        "id": "request-2", "gameName": "诈欺娇娃",
        "createTime": "2026-02-25 22:27:14", "updateTime": "2026-02-25 22:28:09",
        "txStatus": "20", "txPrice": "5.58",
    }, {
        "id": "request-3", "gameName": "异形工厂2",
        "createTime": "2026-02-11 09:38:42", "txStatus": "51", "txPrice": "3.25",
    }]
    requests = parse_steampy_request_orders(request_rows)
    assert [item.status for item in requests] == ["sold", "sold", "status_51"]
    assert requests[0].amount is None and requests[0].fee is None
    assert requests[0].net_amount == Decimal("9.70")
    assert requests[0].occurred_at == "2026-08-24 08:02:06"
    repo.replace_account_orders([purchase, *requests],
                                source_coverage=("ordinary", "request"),
                                source_synced_at={
                                    "sonkwo": "2026-09-30T12:00:00+00:00",
                                    "ordinary": "2026-09-30T12:00:00+00:00",
                                    "request": "2026-10-01T08:00:00+00:00"})
    repo.replace_wallet_bills([
        WalletBill("ak-1", "2026-08-23 12:55:00", Decimal("9.70"),
                   "AK", "request-1", "D"),
        WalletBill("ak-2", "2026-02-25 22:28:00", Decimal("5.58"),
                   "AK", "request-2", "D"),
    ])
    summary = repo.account_order_summary()
    assert summary["source_coverage"] == ["ordinary", "request"]
    assert summary["source_synced_at"]["sonkwo"] == "2026-09-30T12:00:00+00:00"
    assert summary["source_synced_at"]["request"] == "2026-10-01T08:00:00+00:00"
    assert summary["steampy"]["request_sold"] == 2
    assert summary["steampy"]["sale_net"] == "15.28"
    assert summary["steampy"]["gross"] == "0.00"
    assert summary["steampy"]["gross_known_sold"] == 0
    assert summary["unlinked_wallet_credits"]["count"] == 0
    assert summary["missing_wallet_credit_count"] == 0
    assert summary["mismatched_wallet_credit_count"] == 0
    inventory = repo.purchase_inventory()
    assert inventory["summary"]["sold_units"] == 1
    assert inventory["summary"]["gross_unknown_sales"] == 1
    assert inventory["rows"][0]["sale_channel"] == "request"
    assert inventory["rows"][0]["sale_net"] == "9.70"
    assert inventory["rows"][0]["sale_gross"] is None
    assert inventory["rows"][0]["sale_fee"] is None
    assert inventory["rows"][0]["wallet_credit_matches_net"] is True
    assert "SECRET-KEY" not in repo.path.read_bytes().decode("utf-8", errors="ignore")


def test_request_created_before_purchase_but_fulfilled_after_purchase_is_matched():
    """Reproduce the live 19-hour gap between request creation and AK credit."""
    purchase = AccountOrder(
        "sonkwo", "purchase-after-request", "生化奇兵：无限",
        "2026-08-23T20:00:00+00:00", "completed", 1, Decimal("21.62"),
        lines=(PurchaseLine(1, "生化奇兵：无限", (), 1, Decimal("21.62")),))
    request = parse_steampy_request_orders([{
        "id": "request-1", "gameNameCn": "生化奇兵：无限",
        "createTime": "2026-08-23 12:54:51",
        "updateTime": "2026-08-24 08:02:06",
        "txStatus": 20, "txPrice": "9.70",
    }])[0]
    assert reconcile_orders([purchase, request], TitleCatalog())[0].buy_order_id == (
        "purchase-after-request")


@pytest.mark.asyncio
async def test_expired_sonkwo_session_does_not_block_fresh_steampy_sales(tmp_path, monkeypatch):
    settings = Settings(tmp_path)
    repo = Repository(settings.database_path)
    sonkwo, normal, successful = account_rows()
    sonkwo[0]["createdAt"] -= 3 * 86_400_000
    baseline = [*parse_sonkwo_orders(sonkwo), *parse_steampy_orders(normal, successful)]
    old_time = "2026-09-30T00:00:00+00:00"
    repo.replace_account_orders(
        baseline, source_synced_at={"sonkwo": old_time, "ordinary": old_time})
    request = AccountOrder("steampy", "request-1", "游戏甲",
                           "2026-10-01 12:00:00", "sold", 1,
                           None, None, Decimal("5.58"), channel="request")

    async def fetch_one(_settings, _progress, source="both"):
        if source == "sonkwo":
            raise SourceError("HTTP 401")
        assert source == "steampy"
        return [*baseline[1:], request]

    monkeypatch.setattr("autoscout.orders.fetch_account_orders", fetch_one)
    service = OrderSyncService(settings, repo)
    service.start()
    status = await service.wait()
    assert status["status"] == "completed_with_warnings"
    assert "HTTP 401" in status["warnings"][0]
    summary = repo.account_order_summary()
    assert summary["source_coverage"] == ["ordinary", "request"]
    assert summary["source_synced_at"]["sonkwo"] == old_time
    assert summary["source_synced_at"]["request"] != old_time
    assert "HTTP 401" in Repository(settings.database_path).account_order_summary()["sync_warnings"][0]
    assert summary["sonkwo"]["units"] == 2
    assert summary["steampy"]["sold"] == 2
    assert repo.purchase_inventory()["summary"]["sold_units"] == 2


def test_normal_only_order_is_distinguished_from_later_sale_and_wallet_credit(tmp_path):
    """A status-40 record can precede a separate successful order for the same title."""
    settings = Settings(tmp_path)
    repo = Repository(settings.database_path)
    repo.replace_account_orders([
        AccountOrder("steampy", "normal-40", "鬼武者2 (Onimusha 2: Samurai's Destiny)",
                     "2026-06-18 21:12:43", "status_40", 1, None),
        AccountOrder("steampy", "success-later", "鬼武者2", "2026-06-18 21:15:39",
                     "sold", 1, Decimal("95.00"), Decimal("2.85"), Decimal("92.15")),
        AccountOrder("steampy", "normal-51", "异形工厂 2", "2026-02-18 20:27:05",
                     "status_51", 1, None),
    ])
    repo.replace_wallet_bills([
        WalletBill("credit-1", "2026-06-18 21:15:39", Decimal("92.15"),
                   "K", "success-later", "D"),
    ])
    ledger = repo.account_order_ledger(
        platform="steampy", state="not_counted", catalog=TitleCatalog())
    rows = {row["order_id"]: row for row in ledger["rows"]}
    assert ledger["total"] == 2
    assert rows["normal-40"]["later_same_product_sale_count"] == 1
    assert rows["normal-40"]["later_same_product_sales"][0]["order_id"] == "success-later"
    assert rows["normal-40"]["wallet_credit_amount"] is None
    assert rows["normal-51"]["later_same_product_sale_count"] == 0
    sold = repo.account_order_ledger(platform="steampy", state="sold")["rows"]
    assert len(sold) == 1
    assert sold[0]["wallet_credit_matches_net"] is True
    with TestClient(create_app(settings, repo)) as client:
        response = client.get("/api/account-orders/ledger?platform=steampy&state=not_counted")
        assert response.status_code == 200
        assert response.json()["rows"][0]["later_same_product_sale_count"] == 1


def test_reject_unbalanced_purchase_and_unexpected_seller_fee():
    sonkwo, normal, successful = account_rows()
    sonkwo[0]["subtotal"] = 31
    with pytest.raises(SourceError, match="不一致"):
        parse_sonkwo_orders(sonkwo)
    successful[0]["osFee"] = 1
    with pytest.raises(SourceError, match="额外费用"):
        parse_steampy_orders(normal, successful)
