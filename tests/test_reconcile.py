"""Historical first bought, first sold cost allocation regression cases."""

from decimal import Decimal

from autoscout.orders import AccountOrder, PurchaseLine
from autoscout.reconcile import reconcile_orders
from autoscout.repository import Repository
from autoscout.settings import Settings
from autoscout.titles import TitleCatalog


def buy(order_id="buy-1", quantity=1, title="游戏甲", cost="15.00", when="2026-09-30T08:00:00+00:00"):
    return AccountOrder("sonkwo", order_id, title, when, "completed", quantity,
                        Decimal(cost) * quantity,
                        lines=(PurchaseLine(1, title, ("Game A",), quantity, Decimal(cost)),))


def sale(order_id="sale-1", title="Game A", when="2026-09-30 18:00:00", alternatives=()):
    return AccountOrder("steampy", order_id, title, when, "sold", 1,
                        Decimal("25.00"), Decimal("0.75"), Decimal("24.25"),
                        alternate_titles=alternatives)


def test_unique_multilingual_purchase_and_sale_are_candidates(tmp_path):
    orders = [buy(), sale()]
    result = reconcile_orders(orders, TitleCatalog())
    assert len(result) == 1
    assert result[0].status == "candidate"
    assert result[0].buy_order_id == "buy-1"
    assert result[0].buy_unit_cost == Decimal("15.00")
    assert result[0].order_stage_spread == Decimal("9.25")
    repo = Repository(Settings(tmp_path).database_path)
    repo.replace_account_orders(orders)
    summary = repo.account_reconciliations()
    assert summary["ready"] and summary["counts"]["candidate"] == 1
    assert summary["matched_count"] == 1
    assert summary["candidate_order_spread"] == "9.25"
    assert summary["matched_order_spread"] == "9.25"
    assert repo.account_order_lines("sonkwo", "buy-1") == [
        {"line_no": 1, "title": "游戏甲", "quantity": 1, "unit_cost": "15.00",
         "refunded_quantity": 0, "refund_amount": "0.00", "refund_source": ""}]


def test_two_sales_can_use_two_identical_units_from_one_purchase_order():
    results = reconcile_orders([buy(quantity=2), sale("s1"), sale("s2")], TitleCatalog())
    assert {item.status for item in results} == {"candidate"}
    assert {item.buy_unit_no for item in results} == {1, 2}
    assert {item.buy_order_id for item in results} == {"buy-1"}


def test_multiple_purchase_sources_use_fifo_without_ambiguity(tmp_path):
    orders = [buy("b1"), buy("b2"), sale()]
    allocated = reconcile_orders(orders, TitleCatalog())
    assert allocated[0].status == "fifo"
    assert allocated[0].buy_order_id == "b1"
    assert allocated[0].buy_unit_cost == Decimal("15.00")
    assert "先购先销" in allocated[0].reason
    repo = Repository(Settings(tmp_path).database_path)
    repo.replace_account_orders(orders)
    summary = repo.account_reconciliations()
    assert summary["counts"]["fifo"] == 1
    assert summary["matched_count"] == 1
    assert summary["fifo_order_spread"] == "9.25"
    assert summary["matched_order_spread"] == "9.25"
    insufficient = reconcile_orders([buy(), sale("s1"), sale("s2")], TitleCatalog())
    assert [(item.sale_order_id, item.status, item.buy_order_id) for item in insufficient] == [
        ("s1", "candidate", "buy-1"), ("s2", "review", None)]


def test_fifo_allocation_uses_earlier_buy_without_reusing_units():
    orders = [buy("older", cost="10.00", when="2026-09-28T08:00:00+00:00"),
              buy("newer", cost="20.00", when="2026-09-29T08:00:00+00:00"),
              sale("earlier", when="2026-09-29 18:00:00"),
              sale("later", when="2026-09-30 18:00:00")]
    result = {item.sale_order_id: item for item in reconcile_orders(orders, TitleCatalog())}
    assert result["earlier"].status == result["later"].status == "fifo"
    assert result["earlier"].buy_order_id == "older"
    assert result["later"].buy_order_id == "newer"
    assert result["earlier"].order_stage_spread == Decimal("14.25")
    assert result["later"].order_stage_spread == Decimal("4.25")


def test_existing_allocated_review_rows_are_migrated_to_fifo(tmp_path):
    db_path = Settings(tmp_path).database_path
    repo = Repository(db_path)
    repo.replace_account_orders([buy("b1"), buy("b2"), sale()])
    with repo._connect() as db:
        db.execute("""UPDATE account_sale_reconciliation
                   SET status='review', reason='存在 2 个可能的购买订单；按先购先销暂配成本'
                   WHERE sale_order_id='sale-1'""")
    migrated = Repository(db_path).account_reconciliations()
    assert migrated["counts"]["fifo"] == 1
    assert migrated["counts"]["review"] == 0
    assert migrated["matched_order_spread"] == "9.25"
    assert "暂配" not in migrated["rows"][0]["reason"]


def test_sale_before_buy_and_dlc_are_not_automatically_paired():
    early = reconcile_orders([buy(when="2026-10-01T08:00:00+00:00"), sale()], TitleCatalog())
    assert early[0].status == "unmatched"
    dlc = reconcile_orders([buy(title="游戏甲 DLC"), sale(title="游戏甲 DLC")], TitleCatalog())
    assert dlc[0].status == "unmatched"


def test_live_onimusha_bilingual_suffix_matches_both_fifo_purchase_batches():
    orders = [
        AccountOrder("sonkwo", "older", "鬼武者2", "2026-02-27T02:41:00+00:00",
                     "completed", 1, Decimal("86.80"),
                     lines=(PurchaseLine(1, "鬼武者2", ("Onimusha 2: Samurai's Destiny",),
                                         1, Decimal("86.80")),)),
        AccountOrder("sonkwo", "newer", "鬼武者2", "2026-03-14T07:45:16+00:00",
                     "completed", 1, Decimal("86.86"),
                     lines=(PurchaseLine(1, "鬼武者2", ("Onimusha 2: Samurai's Destiny",),
                                         1, Decimal("86.86")),)),
        sale("earlier", "鬼武者2 (Onimusha 2: Samurai's Destiny)", "2026-03-10 18:13:28"),
        sale("later", "鬼武者2 (Onimusha 2: Samurai's Destiny)", "2026-06-18 21:15:39"),
    ]
    matches = {item.sale_order_id: item for item in reconcile_orders(orders, TitleCatalog())}
    assert matches["earlier"].buy_order_id == "older"
    assert matches["later"].buy_order_id == "newer"
    assert all(item.status == "fifo" and "双语括号" in item.reason for item in matches.values())


def test_live_shapez_dlc_full_subtitle_matches_addon_not_base(tmp_path):
    orders = [
        buy("base", title="异形工厂", cost="3.12", when="2026-02-18T05:42:52+00:00"),
        AccountOrder("sonkwo", "addon", "异形工厂 - 谜题挑战者",
                     "2026-02-18T05:42:52+00:00", "completed", 1, Decimal("1.97"),
                     lines=(PurchaseLine(1, "异形工厂 - 谜题挑战者",
                                         ("Shapez.io - Puzzle",), 1, Decimal("1.97")),)),
        sale("sold-addon", "异形工厂dlc- 谜题挑战者", "2026-02-19 23:12:26",
             ("shapez.io Puzzles DLC",)),
    ]
    match = reconcile_orders(orders, TitleCatalog())[0]
    assert match.buy_order_id == "addon"
    assert match.buy_unit_cost == Decimal("1.97")
    assert "DLC 标记" in match.reason
    assert not TitleCatalog().compare_historical("异形工厂 DLC", "异形工厂")[0]
    assert not TitleCatalog().compare_historical("异形工厂 DLC - 另一内容", "异形工厂 - 谜题挑战者")[0]
    assert TitleCatalog({"Shapez.io Puzzles DLC": ["异形工厂 - 谜题挑战者"]}).compare_historical(
        "Shapez.io Puzzles DLC", "异形工厂 - 谜题挑战者")[0]
    repo = Repository(Settings(tmp_path).database_path)
    repo.replace_account_orders(orders)
    synced_at = repo.account_order_summary()["last_synced_at"]
    with repo._connect() as db:
        db.execute("""UPDATE account_sale_reconciliation
                   SET status='unmatched', reason='没有名称和版本一致的杉果购买明细',
                       buy_order_id=NULL,buy_line_no=NULL,buy_unit_no=NULL,
                       buy_unit_cost=NULL,order_stage_spread=NULL""")
    assert repo.reconcile_existing_account_orders(TitleCatalog()) == 1
    assert repo.account_reconciliations()["rows"][0]["buy_order_id"] == "addon"
    assert repo.account_order_summary()["last_synced_at"] == synced_at


def test_conflicting_bilingual_title_and_wrong_edition_stay_unmatched():
    conflict = reconcile_orders([buy(), sale(title="游戏甲 (Game B)")], TitleCatalog())
    assert conflict[0].status == "unmatched"
    edition = reconcile_orders([
        buy(title="游戏甲 豪华版"), sale(title="游戏甲 标准版")], TitleCatalog())
    assert edition[0].status == "unmatched"


def test_confirmed_alias_reconciles_saved_orders_without_refetch(tmp_path):
    repo = Repository(Settings(tmp_path).database_path)
    repo.replace_account_orders([buy(title="空洞骑士"), sale(title="Hollow Knight")])
    assert repo.account_reconciliations()["counts"]["unmatched"] == 1
    repo.reconcile_existing_account_orders(TitleCatalog({"Hollow Knight": ["空洞骑士"]}))
    assert repo.account_reconciliations()["counts"]["candidate"] == 1
