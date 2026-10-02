"""Orders before 2025 stay archived but cannot affect current cash calculations."""

from decimal import Decimal

from autoscout.orders import AccountOrder, PurchaseLine
from autoscout.reconcile import in_report_period
from autoscout.repository import Repository


def _buy(order_id, title, when, quantity, cost):
    amount = Decimal(cost) * quantity
    return AccountOrder("sonkwo", order_id, title, when, "completed", quantity,
                        amount, lines=(PurchaseLine(1, title, (), quantity,
                                                    Decimal(cost)),))


def _sale(order_id, title, when, gross, fee):
    gross, fee = Decimal(gross), Decimal(fee)
    return AccountOrder("steampy", order_id, title, when, "sold", 1,
                        gross, fee, gross - fee)


def test_2025_china_time_boundary_and_archived_orders_do_not_affect_results(tmp_path):
    assert not in_report_period("2024-12-31T15:59:59+00:00")
    assert in_report_period("2024-12-31T16:00:00+00:00")
    assert not in_report_period("2024-12-31 23:59:59")
    assert in_report_period("2025-01-01 00:00:00")

    repo = Repository(tmp_path / "orders.sqlite3")
    repo.replace_account_orders([
        _buy("old-buy", "旧商品", "2024-12-31T15:59:59+00:00", 1, "3.00"),
        _buy("new-buy", "新商品", "2024-12-31T16:00:00+00:00", 2, "5.00"),
        _sale("old-sale", "新商品", "2024-12-31 23:59:59", "9.00", "1.00"),
        _sale("new-sale", "新商品", "2025-01-01 00:01:00", "11.00", "1.00"),
        _sale("old-only-sale", "旧商品", "2025-01-02 00:00:00", "7.00", "1.00"),
    ])

    with repo._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM account_orders").fetchone()[0] == 5
    summary = repo.account_order_summary()
    assert summary["sonkwo"] == {"orders": 1, "completed": 1, "units": 2,
                                  "spent": "10.00", "paid": "10.00",
                                  "refund_amount": "0.00", "refunded_units": 0,
                                  "refunded_orders": 0}
    assert summary["steampy"]["orders"] == 2
    assert summary["steampy"]["gross"] == "18.00"
    assert repo.account_order_ledger()["total"] == 3
    assert len(repo.account_orders("sonkwo")) == 1
    assert repo.account_orders("sonkwo")[0]["order_id"] == "new-buy"
    assert repo.account_order_lines("sonkwo", "old-buy") == []

    matches = {row["sale_order_id"]: row for row in
               repo.account_reconciliations()["rows"]}
    assert set(matches) == {"new-sale", "old-only-sale"}
    assert matches["new-sale"]["buy_order_id"] == "new-buy"
    assert matches["old-only-sale"]["status"] == "unmatched"

    inventory = repo.purchase_inventory()
    assert inventory["report_start"] == "2025-01-01"
    assert inventory["summary"]["bought_units"] == 2
    assert inventory["summary"]["sold_units"] == 1
    assert inventory["summary"]["unsold_units"] == 1
    assert inventory["total"] == 2
    assert {row["purchase_order_id"] for row in inventory["rows"]} == {"new-buy"}

    with repo._connect() as db:
        db.execute("""UPDATE account_sale_reconciliation SET buy_order_id='old-buy',
                   buy_line_no=1,buy_unit_no=1,buy_unit_cost='3.00'
                   WHERE sale_order_id='new-sale'""")
    assert repo.reconcile_existing_account_orders() == 2
    refreshed = {row["sale_order_id"]: row for row in
                 repo.account_reconciliations()["rows"]}
    assert refreshed["new-sale"]["buy_order_id"] == "new-buy"
