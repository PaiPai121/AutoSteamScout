"""Mixed purchase orders must be searchable by every product, not just the header."""

from decimal import Decimal

from fastapi.testclient import TestClient

from autoscout.orders import AccountOrder, PurchaseLine
from autoscout.repository import Repository
from autoscout.settings import Settings
from autoscout.web import create_app


def test_sworn_search_returns_both_single_and_mixed_purchase_orders(tmp_path):
    settings = Settings(tmp_path)
    repo = Repository(settings.database_path)
    repo.replace_account_orders([
        AccountOrder("sonkwo", "16261563", "圣杯誓约", "2026-02-18T17:53:24+00:00",
                     "completed", 1, Decimal("18.75"), lines=(
                         PurchaseLine(1, "圣杯誓约", ("SWORN",), 1, Decimal("18.75")),)),
        AccountOrder("sonkwo", "16256369", "异形工厂 等 2 件", "2026-02-18T12:24:21+00:00",
                     "completed", 2, Decimal("20.25"), lines=(
                         PurchaseLine(1, "异形工厂", ("Shapez.io",), 1, Decimal("3.19")),
                         PurchaseLine(2, "圣杯誓约", ("SWORN",), 1, Decimal("17.06")))),
    ])
    for term in ("圣杯誓约", "sworn"):
        result = repo.account_order_ledger(query=term)
        assert result["total"] == 2
        assert {row["order_id"] for row in result["rows"]} == {"16261563", "16256369"}
    mixed = next(row for row in result["rows"] if row["order_id"] == "16256369")
    assert mixed["purchase_lines"] == [
        {"line_no": 1, "title": "异形工厂", "quantity": 1, "unit_cost": "3.19",
         "refunded_quantity": 0, "refund_amount": "0.00", "refund_source": ""},
        {"line_no": 2, "title": "圣杯誓约", "quantity": 1, "unit_cost": "17.06",
         "refunded_quantity": 0, "refund_amount": "0.00", "refund_source": ""},
    ]
    assert repo.account_order_ledger(query="16256369")["total"] == 1
    with TestClient(create_app(settings, repo)) as client:
        result = client.get("/api/account-orders/ledger", params={"query": "圣杯誓约"}).json()
        assert result["total"] == 2
