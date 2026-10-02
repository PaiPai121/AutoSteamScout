"""Historical purchase-to-sale cost allocation by product and purchase order."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

from .domain import money
from .titles import TitleCatalog

if TYPE_CHECKING:
    from .orders import AccountOrder, PurchaseLine


CHINA_TIME = timezone(timedelta(hours=8))
REPORT_START_CHINA = datetime(2025, 1, 1, tzinfo=CHINA_TIME)


@dataclass(frozen=True, slots=True)
class SaleReconciliation:
    sale_order_id: str
    status: str
    reason: str
    buy_order_id: str | None = None
    buy_line_no: int | None = None
    buy_unit_no: int | None = None
    buy_unit_cost: Decimal | None = None
    order_stage_spread: Decimal | None = None


@dataclass(frozen=True, slots=True)
class _Unit:
    order: AccountOrder
    line: PurchaseLine
    unit_no: int


def _time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(CHINA_TIME) if parsed.tzinfo else parsed.replace(tzinfo=CHINA_TIME)
    except ValueError:
        return None


def in_report_period(value: str) -> bool:
    """Account-order reporting begins at 2025-01-01 00:00 China time."""
    parsed = _time(value)
    return parsed is not None and parsed >= REPORT_START_CHINA


def _same_product(unit: _Unit, sale: AccountOrder, catalog: TitleCatalog) -> tuple[bool, str]:
    return catalog.compare_historical_products(
        unit.line.title, unit.line.alternate_titles,
        sale.title, sale.alternate_titles)


def reconcile_orders(orders: list[AccountOrder], catalog: TitleCatalog) -> list[SaleReconciliation]:
    """Pair available historical purchases to sales using first bought, first sold.

    Multiple eligible purchase orders are a deterministic accounting allocation,
    not an unresolved match. No keys or raw responses are read by this function.
    """
    units = [_Unit(order, line, unit_no)
             for order in orders if order.platform == "sonkwo" and order.status == "completed"
             and in_report_period(order.occurred_at)
             for line in order.lines
             for unit_no in range(1, line.quantity - line.refunded_quantity + 1)]
    sales = [order for order in orders if order.platform == "steampy"
             and order.status == "sold" and in_report_period(order.occurred_at)]
    edges: dict[str, set[int]] = {}
    match_reasons: dict[tuple[str, int], str] = {}
    basic_reasons: dict[str, str] = {}
    for sale in sales:
        sale_time = _time(sale.occurred_at)
        if sale_time is None:
            edges[sale.order_id] = set()
            basic_reasons[sale.order_id] = "卖出时间无法核对"
            continue
        name_matches = []
        for index, unit in enumerate(units):
            matched, reason = _same_product(unit, sale, catalog)
            if matched:
                name_matches.append(index)
                match_reasons[(sale.order_id, index)] = reason
        eligible = {index for index in name_matches
                    if (buy_time := _time(units[index].order.occurred_at)) is not None
                    and buy_time <= sale_time}
        edges[sale.order_id] = eligible
        if not name_matches:
            basic_reasons[sale.order_id] = "没有名称和版本一致的杉果购买明细"
        elif not eligible:
            basic_reasons[sale.order_id] = "同名买入晚于卖出，或买入时间无法核对"

    by_id = {sale.order_id: sale for sale in sales}
    output: dict[str, SaleReconciliation] = {
        sale_id: SaleReconciliation(sale_id, "unmatched", basic_reasons[sale_id])
        for sale_id, choices in edges.items() if not choices
    }
    remaining = set(edges) - set(output)
    while remaining:
        start = next(iter(remaining))
        component_sales = {start}
        component_units = set(edges[start])
        while True:
            linked = {sale_id for sale_id in remaining
                      if edges[sale_id] & component_units}
            new_units = set().union(*(edges[sale_id] for sale_id in linked)) if linked else set()
            if linked == component_sales and new_units == component_units:
                break
            component_sales, component_units = linked, new_units
        remaining.difference_update(component_sales)
        source_orders = {units[index].order.order_id for index in component_units}
        costs = {units[index].line.unit_cost for index in component_units}
        # Resolve constrained sales first, then prefer older purchase units.
        # Keep a maximal allocation even when some sales exceed available buys.
        chosen: dict[int, str] = {}

        def assign(sale_id: str, visited: set[int]) -> bool:
            options = sorted(edges[sale_id], key=lambda index: (
                _time(units[index].order.occurred_at), units[index].order.order_id,
                units[index].line.line_no, units[index].unit_no))
            for index in options:
                if index not in chosen and index not in visited:
                    visited.add(index)
                    chosen[index] = sale_id
                    return True
            for index in options:
                if index in visited:
                    continue
                visited.add(index)
                if assign(chosen[index], visited):
                    chosen[index] = sale_id
                    return True
            return False

        sale_ids = sorted(component_sales, key=lambda sale_id: (
            len(edges[sale_id]), _time(by_id[sale_id].occurred_at), sale_id))
        for sale_id in sale_ids:
            assign(sale_id, set())
        status = "candidate" if len(source_orders) == 1 and len(costs) == 1 else "fifo"
        for index, sale_id in chosen.items():
            sale = by_id[sale_id]
            unit = units[index]
            allocation = ("单一购买来源计入成本" if status == "candidate"
                          else "按先购先销计入成本")
            reason = f"{match_reasons[(sale_id, index)]}；{allocation}"
            output[sale_id] = SaleReconciliation(
                sale_id, status, reason, unit.order.order_id,
                unit.line.line_no, unit.unit_no, unit.line.unit_cost,
                money(sale.net_amount - unit.line.unit_cost))
        reason = ("同款卖出多于可用买入件数" if len(component_units) < len(component_sales)
                  else "同名订单之间无法建立不重复的一对一配对")
        for sale_id in component_sales - set(chosen.values()):
            output[sale_id] = SaleReconciliation(sale_id, "review", reason)
    return [output[sale.order_id] for sale in sales]
