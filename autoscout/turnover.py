"""Personal capital turnover from the full purchase ledger, including unsold units.

Buyer order creation is never a listing timestamp. FIFO gives a capital holding
period, not proof of the actual key's identity or its continuously listed time.
Closed purchase cohorts prevent quick recent sales from inflating 7/30-day rates.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from statistics import median

from .demand import CHINA, timestamp
from .domain import PricingPolicy, project_cash
from .products import ProductIdentity
from .titles import TitleCatalog

DAY = 86400


def _days(seconds: float) -> float:
    return round(seconds / DAY, 2)


def _middle(values: list[float]) -> float | None:
    return round(median(values), 2) if values else None


def _percentage(numerator: int, denominator: int) -> str | None:
    return str((Decimal(numerator) * 100 / denominator).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) if denominator else None


def _summary(units: list[dict], as_of: datetime, complete: bool) -> dict:
    sold = [unit for unit in units if unit["sale_date"] is not None]
    remaining = [unit for unit in units if unit["sale_date"] is None]
    periods = [(unit["sale_date"] - unit["purchase_date"]).total_seconds() / DAY for unit in sold]
    stock_periods = [unit["stock_seconds"] / DAY for unit in sold if unit["stock_seconds"] is not None]
    windows = {}
    for days in (7, 30):
        # Exclude all immature purchases, including those already sold quickly.
        mature = [unit for unit in units if (as_of - unit["purchase_date"]).total_seconds() >= days * DAY]
        count = sum(unit["sale_date"] is not None and
                    (unit["sale_date"] - unit["purchase_date"]).total_seconds() <= days * DAY for unit in mature)
        windows[str(days)] = {"eligible_units": len(mature), "matched_in_window": count,
                              "immature_units": len(units) - len(mature),
                              "matched_percent": _percentage(count, len(mature)) if complete else None}
    recent = {str(days): sum(unit["sale_date"] >= as_of - timedelta(days=days) for unit in sold) for days in (7, 30)}
    last = max(sold, key=lambda unit: unit["sale_date"], default=None)
    old_remaining = [unit for unit in remaining if (as_of - unit["purchase_date"]).total_seconds() >= 30 * DAY]
    recent_prices = sorted(Decimal(unit["sale_gross"]) for unit in sold
                           if unit["sale_date"] >= as_of - timedelta(days=30) and unit.get("sale_gross") is not None)
    # A conservative observed price; not a forecast or a new sellability gate.
    lower_quartile = recent_prices[(len(recent_prices) - 1) // 4] if len(recent_prices) >= 3 else None
    return {"bought_units": len(units), "matched_sold_units": len(sold), "unmatched_units": len(remaining),
            "completed_median_days": _middle(periods), "stock_to_sale_median_days": _middle(stock_periods),
            "stock_time_samples": len(stock_periods), "windows": windows,
            "own_sales_7d": recent["7"], "own_sales_30d": recent["30"],
            "last_sale_at": last["sale_date"].isoformat(timespec="seconds") if last else None,
            "last_sale_gross": last.get("sale_gross") if last else None,
            "last_sale_net": last.get("sale_net") if last else None,
            "last_sale_channel": last.get("sale_channel") if last else None,
            "oldest_unmatched_days": max((_days((as_of - unit["purchase_date"]).total_seconds()) for unit in remaining), default=None),
            "unmatched_over_30d": len(old_remaining),
            "unmatched_cost": str(sum((Decimal(unit["unit_cost"]) for unit in remaining), Decimal("0")).quantize(Decimal("0.01"))),
            "gross_price_samples_30d": len(recent_prices),
            "observed_lower_quartile_gross_30d": str(lower_quartile) if lower_quartile is not None else None}


def build_turnover_report(snapshot: dict, catalog: TitleCatalog, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    sources = snapshot.get("source_synced_at") or {}
    required = ["sonkwo", "ordinary"]
    if "request" in snapshot.get("source_coverage", []):
        required.append("request")
    times, issues = [], []
    for source in required:
        try:
            times.append(timestamp(sources[source]))
        except (KeyError, TypeError, ValueError):
            issues.append(f"{source}缺少有效同步时间")
    as_of = min(now, *times) if times else now
    complete = not issues and "request" in snapshot.get("source_coverage", [])
    if not complete:
        issues.append("未覆盖全部普通与求购订单；成交比例仅保留已配对件数")
    if snapshot.get("summary", {}).get("unallocated_sales"):
        issues.append("有成功销售尚未分配到购买，已配对比例是当前确认的下限")
    groups, units = [], []
    for raw in snapshot.get("rows", []):
        unit = dict(raw)
        try:
            purchased = timestamp(unit["purchase_time"])
            if purchased > as_of:
                continue
            sale = timestamp(unit["sale_time"]) if unit.get("sale_time") else None
            if sale and sale > as_of:
                sale = None
            if sale and sale < purchased:
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            issues.append("有购买或成交时间无效，已排除该件周转样本")
            continue
        stock_seconds = None
        if sale and unit.get("stock_created_at"):
            try:
                stock_time = timestamp(unit["stock_created_at"])
                if stock_time > sale:
                    raise ValueError()
                stock_seconds = (sale - stock_time).total_seconds()
            except (TypeError, ValueError):
                issues.append("有平台库存记录时间无效，未计算该件库存周转")
        unit.update(purchase_date=purchased, sale_date=sale, stock_seconds=stock_seconds)
        # Same source SKU plus compatible full official names. Legacy rows
        # without IDs may group only by the existing conservative title rules.
        group = next((group for group in groups
                      if group["product_id"] == unit.get("product_id") and
                      catalog.compare_historical_products(group["title"], tuple(group["alternate_titles"]),
                                                          unit["title"], tuple(unit.get("alternate_titles", [])))[0]), None)
        if group is None:
            group = {"title": unit["title"], "alternate_titles": unit.get("alternate_titles", []),
                     "product_id": unit.get("product_id"), "_units": []}
            groups.append(group)
        group["_units"].append(unit)
        units.append(unit)
    products = []
    for group in groups:
        ids = sorted({unit["market_product_id"] for unit in group["_units"] if unit.get("market_product_id") and unit["sale_date"]})
        products.append({**group, "market_product_ids": ids, **_summary(group["_units"], as_of, complete)})
    products.sort(key=lambda row: (row["unmatched_over_30d"], row["oldest_unmatched_days"] or 0,
                                   row["completed_median_days"] or 0), reverse=True)
    weeks = []
    end = as_of.astimezone(CHINA).replace(hour=0, minute=0, second=0, microsecond=0)
    monday = end - timedelta(days=end.weekday())
    for index in range(11, -1, -1):
        start = monday - timedelta(days=index * 7)
        finish = min(start + timedelta(days=7), as_of)
        sold = [unit for unit in units if unit["sale_date"] and start <= unit["sale_date"] < finish]
        weeks.append({"start": start.date().isoformat(), "through": finish.isoformat(timespec="seconds"),
                      "partial": finish < start + timedelta(days=7), "sold_units": len(sold),
                      "sale_net": str(sum((Decimal(unit["sale_net"]) for unit in sold), Decimal("0")).quantize(Decimal("0.01")))})
    return {"ready": snapshot.get("ready", False), "model": "personal_closed_purchase_cohorts_v1",
            "scope": "own_account", "as_of": as_of.isoformat(timespec="seconds"),
            "source_synced_at": sources, "coverage_complete": complete,
            "stale": (now - as_of).total_seconds() > 3600,
            "report_start": snapshot.get("report_start"),
            "issues": list(dict.fromkeys([*snapshot.get("sync_warnings", []), *issues])),
            "summary": _summary(units, as_of, complete), "products": products, "weeks": weeks,
            "note": "按先购先销统计购入到成交的资金占用时间，含上架前时间。7/30天比例只统计已购满该天数的整批购买，未配对件仍计入分母；这不是全市场销量或未来售出天数。"}


def public_report(report: dict) -> dict:
    return {**report, "products": [{key: value for key, value in product.items() if key != "_units"}
                                   for product in report["products"]]}


def candidate_history(offer: dict | None, market: dict | None, report: dict,
                      catalog: TitleCatalog, cost: Decimal, policy: PricingPolicy) -> dict:
    result = {"scope": "own_account", "as_of": report["as_of"], "stale": report["stale"],
              "coverage_complete": report["coverage_complete"], "status": "no_history",
              "reason": "尚无已按商品编号与完整名称核对的个人进货样本"}
    if not offer or not market or not offer.get("detail_checked") or not market.get("detail_checked"):
        return result
    try:
        source, target = ProductIdentity.from_snapshot(offer), ProductIdentity.from_snapshot(market)
    except (ValueError, TypeError):
        return result
    if source.market_region != "cn" or target.market_region != "cn":
        return result
    matching = []
    for group in report["products"]:
        if not catalog.compare_historical_products(source.names[0], source.names[1:], group["title"], tuple(group["alternate_titles"]))[0]:
            continue
        if group["market_product_ids"] and group["market_product_ids"] != [target.product_id]:
            continue
        if group["product_id"] != source.product_id and target.product_id not in group["market_product_ids"]:
            continue
        # A historical game ID must still agree with the current exact variant.
        for unit in group["_units"]:
            if unit["sale_date"] and (unit.get("market_product_id") != target.product_id or
                                      not catalog.compare_historical_products(unit["title"], tuple(unit.get("alternate_titles", [])),
                                                                              target.names[0], target.names[1:])[0]):
                break
        else:
            matching.extend(group["_units"])
    if not matching:
        return result
    summary = _summary(matching, timestamp(report["as_of"]), report["coverage_complete"])
    lower_price = summary["observed_lower_quartile_gross_30d"]
    if lower_price is not None:
        profit, roi, pricing = project_cash(cost, Decimal(lower_price), policy)
        summary.update(observed_price_profit=str(profit), observed_price_roi=str(roi), observed_price_pricing=pricing)
    return {**result, **summary, "status": "personal_history", "product_id": target.product_id,
            "reason": "同款个人购买与成交样本；按先购先销分配，不据此承诺未来销量或上架等待天数"}
