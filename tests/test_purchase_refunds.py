"""Completed Sonkwo orders can contain successfully refunded individual items."""

from dataclasses import replace
from decimal import Decimal

from fastapi.testclient import TestClient
import pytest

from autoscout.orders import AccountOrder, PurchaseLine, parse_sonkwo_orders
from autoscout.payouts import WalletBill
from autoscout.ports import SourceError
from autoscout.repository import Repository
from autoscout.settings import Settings
from autoscout.web import create_app


def sworn_orders(refunded=False):
    quantity = 1 if refunded else 0
    source = "platform" if refunded else ""
    return [
        AccountOrder("sonkwo", "16261563", "圣杯誓约", "2026-02-18T17:53:24+00:00",
                     "completed", 1, Decimal("18.75"), lines=(
                         PurchaseLine(1, "圣杯誓约", ("SWORN",), 1, Decimal("18.75"),
                                      quantity, Decimal("18.75") if refunded else Decimal("0"), source),)),
        AccountOrder("sonkwo", "16256369", "异形工厂 等 2 件", "2026-02-18T12:24:21+00:00",
                     "completed", 2, Decimal("20.25"), lines=(
                         PurchaseLine(1, "异形工厂", ("shapez.io",), 1, Decimal("3.19")),
                         PurchaseLine(2, "圣杯誓约", ("SWORN",), 1, Decimal("17.06"),
                                      quantity, Decimal("17.06") if refunded else Decimal("0"), source))),
    ]


def test_sonkwo_suborder_refund_status_survives_import(tmp_path):
    # Original failing contract: the parent is completed while an individual
    # subOrders[].status is refunded. The mixed order still contains shapez.
    source = [
        {"id": "16261563", "state": "completed", "createdAt": 1771437204000,
         "subtotal": "18.75", "subOrders": [
             {"status": 2, "fulfillStatus": 0, "quantity": 1, "realPrice": "18.75",
              "sku": {"chsName": "圣杯誓约", "enName": "SWORN"}}]},
        {"id": "16256369", "state": "completed", "createdAt": 1771417461000,
         "subtotal": "20.25", "subOrders": [
             {"status": 1, "fulfillStatus": 1, "quantity": 1, "realPrice": "3.19",
              "sku": {"chsName": "异形工厂", "enName": "shapez.io"}},
             {"status": 2, "fulfillStatus": 0, "quantity": 1, "realPrice": "17.06",
              "sku": {"chsName": "圣杯誓约", "enName": "SWORN"}}]},
    ]
    parsed = parse_sonkwo_orders(source)
    assert parsed[0].lines[0].refunded_quantity == 1
    assert parsed[1].lines[0].refunded_quantity == 0
    assert parsed[1].lines[1].refund_source == "platform"
    assert parsed[1].lines[1].refund_amount == Decimal("17.06")
    repo = Repository(Settings(tmp_path).database_path)
    repo.replace_account_orders(parsed)
    assert repo.account_order_summary()["sonkwo"]["spent"] == "3.19"
    assert repo.purchase_inventory()["summary"]["bought_units"] == 1
    assert repo.purchase_inventory(query="SWORN")["total"] == 0

    # Applying for a refund is not a completed refund.
    source[0]["subOrders"][0]["status"] = 3
    assert parse_sonkwo_orders(source)[0].lines[0].refunded_quantity == 0


@pytest.mark.parametrize("status", [None, 7, True, "unrecognized"])
def test_unknown_item_refund_status_does_not_publish_wrong_inventory(tmp_path, status):
    repo = Repository(Settings(tmp_path).database_path)
    repo.replace_account_orders(sworn_orders(True))
    with pytest.raises(SourceError, match="状态缺失或未知"):
        parse_sonkwo_orders([{
            "id": "changed-contract", "state": "completed", "createdAt": 1771437204000,
            "subtotal": "18.75", "subOrders": [{"status": status, "quantity": 1,
                "realPrice": "18.75", "sku": {"chsName": "圣杯誓约"}}],
        }])
    assert repo.financial_overview()["purchase_spent"] == "3.19"


def test_refunded_items_are_excluded_even_when_order_stays_completed(tmp_path):
    settings = Settings(tmp_path)
    repo = Repository(settings.database_path)
    sale = AccountOrder("steampy", "sworn-sale", "圣杯誓约", "2026-02-20 12:00:00",
                        "sold", 1, Decimal("25"), Decimal("0.75"), Decimal("24.25"))
    repo.replace_account_orders([*sworn_orders(True), sale])
    summary = repo.account_order_summary()["sonkwo"]
    assert summary == {"orders": 2, "completed": 1, "units": 1, "spent": "3.19",
                       "paid": "39.00", "refund_amount": "35.81", "refunded_units": 2,
                       "refunded_orders": 2}
    assert repo.purchase_inventory(query="圣杯誓约")["total"] == 0
    assert repo.purchase_inventory()["rows"][0]["title"] == "异形工厂"
    assert repo.account_reconciliations()["counts"]["unmatched"] == 1
    assert repo.account_order_ledger(state="completed_buy")["total"] == 1
    ledger = {row["order_id"]: row for row in repo.account_order_ledger(platform="sonkwo")["rows"]}
    assert ledger["16261563"]["purchase_display_status"] == "refunded"
    assert ledger["16261563"]["purchase_net_spent"] == "0.00"
    assert ledger["16256369"]["purchase_display_status"] == "partially_refunded"
    assert ledger["16256369"]["purchase_net_spent"] == "3.19"
    with TestClient(create_app(settings, repo)) as client:
        assert client.get("/api/account-orders/inventory?query=圣杯誓约").json()["total"] == 0
        assert len(client.get("/api/account-orders/refunds").json()) == 2
        assert client.get("/api/finance/overview").json()["purchase_spent"] == "3.19"


def test_user_confirmed_refunds_survive_resync_without_changing_source_time(tmp_path):
    repo = Repository(Settings(tmp_path).database_path)
    source = sworn_orders()
    repo.replace_account_orders(source)
    before = repo.account_order_summary()
    assert repo.purchase_inventory(query="圣杯誓约")["total"] == 2
    for order_id, line_no, amount in (("16261563", 1, "18.75"), ("16256369", 2, "17.06")):
        repo.confirm_purchase_refund(order_id, line_no, 1, Decimal(amount), "用户核对杉果：退款成功")
    after = repo.account_order_summary()
    assert after["last_synced_at"] == before["last_synced_at"]
    assert after["source_synced_at"] == before["source_synced_at"]
    assert after["sonkwo"]["spent"] == "3.19"
    assert after["refunds_pending_platform_verification"] == 2
    repo = Repository(repo.path)
    assert repo.saved_account_orders("sonkwo")[0].lines[0].refunded_quantity == 1
    repo.replace_account_orders(source)
    assert repo.purchase_inventory(query="圣杯誓约")["total"] == 0
    assert repo.account_order_summary()["sonkwo"]["spent"] == "3.19"
    # A later platform-confirmed snapshot supersedes the manual provenance.
    repo.replace_account_orders(sworn_orders(True))
    assert repo.account_order_summary()["refunds_pending_platform_verification"] == 0
    # Reordered or changed lines must not receive an unrelated saved refund.
    changed = replace(source[0], lines=(replace(source[0].lines[0], title="其他商品"),))
    with pytest.raises(ValueError, match="明细已改变"):
        repo.replace_account_orders([changed, source[1]])
    assert repo.account_order_summary()["sonkwo"]["spent"] == "3.19"


def test_partial_quantity_refund_preserves_remaining_unit_and_cash_totals(tmp_path):
    repo = Repository(Settings(tmp_path).database_path)
    repo.replace_account_orders([
        AccountOrder("sonkwo", "two-units", "游戏", "2026-02-18T12:00:00+00:00", "completed", 2,
                     Decimal("20"), lines=(PurchaseLine(1, "游戏", (), 2, Decimal("10"),
                                                        1, Decimal("10"), "platform"),)),
    ])
    assert repo.purchase_inventory()["summary"]["bought_units"] == 1
    assert repo.account_order_summary()["sonkwo"]["spent"] == "10.00"
    repo.replace_wallet_bills([
        WalletBill("old", "2024-12-31 12:00:00", Decimal("-40"), "CashOut", "old-id", "C"),
        WalletBill("old-fee", "2024-12-31 12:00:00", Decimal("-0.40"), "CashOutFee", "old-id", "C"),
        WalletBill("w1", "2026-03-14 16:21:19", Decimal("-460"), "CashOut", "w1-id", "C"),
        WalletBill("f1", "2026-03-14 16:21:19", Decimal("-4.60"), "CashOutFee", "w1-id", "C"),
        WalletBill("w2", "2026-08-30 13:35:27", Decimal("-301"), "CashOut", "w2-id", "C"),
        WalletBill("f2", "2026-08-30 13:35:27", Decimal("-3.01"), "CashOutFee", "w2-id", "C"),
    ])
    overview = repo.financial_overview()
    assert overview["withdrawal_debits"] == "761.00"
    assert overview["withdrawal_fees"] == "7.61"
    assert overview["withdrawal_count"] == overview["bank_pending_count"] == 2
    assert overview["bank_confirmed"] is None
    repo.record_bank_receipt("w2", Decimal("301"), "2026-08-31")
    overview = repo.financial_overview()
    assert overview["bank_confirmed"] == "301.00"
    assert overview["bank_confirmed_count"] == overview["bank_pending_count"] == 1


def test_full_order_refund_migrates_legacy_snapshot_and_respects_cutoff(tmp_path):
    repo = Repository(Settings(tmp_path).database_path)
    parsed = parse_sonkwo_orders([
        {"id": "fully-refunded", "state": "refunded", "createdAt": 1771437204000,
         "subtotal": "20", "subOrders": [
             {"quantity": 2, "realPrice": "10", "sku": {"chsName": "游戏"}}]},
        {"id": "old-refunded", "state": "refunded", "createdAt": 1704067200000,
         "subtotal": "100", "subOrders": [
             {"status": "refunded", "quantity": 1, "realPrice": "100", "sku": {"chsName": "旧游戏"}}]},
    ])
    assert parsed[0].lines[0].refunded_quantity == 2
    repo.replace_account_orders(parsed)
    original_sync = repo.account_order_summary()["last_synced_at"]
    with repo._connect() as db:
        db.execute("UPDATE account_order_lines SET refunded_quantity=0,refund_amount='0.00',refund_source=''")
    repo = Repository(repo.path)
    overview = repo.financial_overview()
    assert overview["purchase_paid"] == overview["purchase_refunds"] == "20.00"
    assert overview["purchase_spent"] == "0.00"
    assert overview["purchase_units"] == 0 and overview["refunded_units"] == 2
    assert repo.account_order_summary()["last_synced_at"] == original_sync
    assert len(repo.refunded_purchases()) == 1
    assert repo.account_order_ledger()["rows"][0]["purchase_net_spent"] == "0.00"
