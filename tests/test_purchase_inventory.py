"""A purchase remains visible when only some units sell, even beside failed listings."""

from decimal import Decimal

from fastapi.testclient import TestClient

from autoscout.orders import AccountOrder, PurchaseLine, parse_sonkwo_orders
from autoscout.payouts import WalletBill
from autoscout.repository import Repository
from autoscout.settings import Settings
from autoscout.web import create_app


def test_purchase_inventory_tracks_sold_loss_and_unmatched_cost(tmp_path):
    settings = Settings(tmp_path)
    repo = Repository(settings.database_path)
    repo.replace_account_orders([
        AccountOrder("sonkwo", "buy-a", "游戏甲", "2026-09-28T08:00:00+00:00",
                     "completed", 2, Decimal("20.00"),
                     lines=(PurchaseLine(1, "游戏甲", (), 2, Decimal("10.00")),)),
        AccountOrder("sonkwo", "buy-b", "游戏乙", "2026-09-28T08:00:00+00:00",
                     "completed", 1, Decimal("5.00"),
                     lines=(PurchaseLine(1, "游戏乙", (), 1, Decimal("5.00")),)),
        AccountOrder("steampy", "attempt-a", "游戏甲", "2026-09-29 09:00:00",
                     "status_40", 1, None),
        AccountOrder("steampy", "sale-a", "游戏甲", "2026-09-29 10:00:00",
                     "sold", 1, Decimal("10.00"), Decimal("2.00"), Decimal("8.00")),
        AccountOrder("steampy", "sale-b", "游戏乙", "2026-09-29 11:00:00",
                     "sold", 1, Decimal("11.00"), Decimal("1.00"), Decimal("10.00")),
        AccountOrder("steampy", "sale-without-buy", "游戏丙", "2026-09-29 12:00:00",
                     "sold", 1, Decimal("5.00"), Decimal("0.00"), Decimal("5.00")),
    ])
    repo.replace_wallet_bills([
        WalletBill("credit-a", "2026-09-29 10:00:00", Decimal("8.00"),
                   "K", "sale-a", "D"),
    ])
    inventory = repo.purchase_inventory(page_size=1)
    assert inventory["ready"] and inventory["total"] == 3 and inventory["pages"] == 3
    assert inventory["report_start"] == "2025-01-01"
    assert inventory["summary"] == {
        "product_count": 2, "bought_units": 3, "sold_units": 2,
            "unsold_units": 1, "loss_units": 1, "unallocated_sales": 1,
            "gross_unknown_sales": 0,
        "purchase_total": "25.00", "sold_cost": "15.00", "unsold_cost": "10.00",
        "sale_gross": "21.00", "sale_fees": "3.00", "sale_net": "18.00",
        "order_stage_spread": "3.00"}
    game_a = repo.purchase_inventory(state="loss")["rows"][0]
    assert game_a["title"] == "游戏甲"
    assert game_a["purchase_order_id"] == "buy-a" and game_a["unit_no"] == 1
    assert game_a["purchase_quantity"] == 2 and game_a["purchase_unit_no"] == 1
    assert game_a["unit_cost"] == "10.00" and game_a["order_stage_spread"] == "-2.00"
    assert game_a["sale_order_id"] == "sale-a"
    assert game_a["wallet_credit_matches_net"] is True
    assert repo.purchase_inventory(state="unsold")["total"] == 1
    assert repo.purchase_inventory(state="sold")["total"] == 2
    assert repo.purchase_inventory(query="sale-b")["rows"][0]["title"] == "游戏乙"
    assert repo.purchase_inventory(query="attempt-a")["total"] == 0
    assert repo.account_order_ledger(state="not_counted")["total"] == 1
    with TestClient(create_app(settings, repo)) as client:
        response = client.get("/api/account-orders/inventory?state=unsold")
        assert response.status_code == 200
        assert response.json()["rows"][0]["title"] == "游戏甲"
        assert client.get("/api/account-orders/inventory?state=unknown").status_code == 400


def test_second_product_in_purchase_order_is_one_victoria_unit(tmp_path):
    """The reported order's second product is an ordinal, not a quantity of two."""
    repo = Repository(Settings(tmp_path).database_path)
    orders = parse_sonkwo_orders([{
        "id": "16448944", "state": "completed", "createdAt": 1773474316000,
        "subtotal": "103.33", "subOrders": [
            {"status": 1, "quantity": 1, "realPrice": "86.86",
             "sku": {"chsName": "鬼武者2", "enName": "Onimusha 2: Samurai's Destiny"}},
            {"status": 1, "quantity": 1, "realPrice": "16.47",
             "sku": {"enName": "Victoria 3: Dawn of Wonder"}},
        ],
    }])
    repo.replace_account_orders(orders)
    victoria = repo.purchase_inventory(query="Victoria")
    assert victoria["total"] == 1
    assert victoria["rows"][0]["line_no"] == 2
    assert victoria["rows"][0]["purchase_quantity"] == 1
    assert victoria["rows"][0]["purchase_unit_no"] == 1
    assert victoria["rows"][0]["unit_cost"] == "16.47"
    assert repo.purchase_inventory()["summary"]["bought_units"] == 2
