"""Public supply and open buy requests, kept separate from completed sales.

SteamPy's official seller component shows ``sold`` next to a seller's name.
Neither it nor ``keySales`` carries a dated, product-specific trade window.
We deliberately do not derive turnover or recent sales from those fields,
vanished listings, stock changes, or disappeared buy requests.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import parse_qs, urlsplit

from .ports import SourceError
from .products import ProductIdentity

CHINA = timezone(timedelta(hours=8))
MAX_BOOK_PAGES = 3
HISTORY_DAYS = 7
MIN_TREND_SECONDS = 1800


def timestamp(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("时间必须是平台提供的日期字符串")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return result.replace(tzinfo=CHINA) if result.tzinfo is None else result


def _integer(value, label: str) -> int:
    if isinstance(value, bool) or not str(value).isdigit():
        raise SourceError(f"SteamPy {label}无效")
    return int(value)


def _price(value) -> Decimal:
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result <= 0 or result != result.quantize(Decimal("0.01")):
            raise ValueError()
        return result.quantize(Decimal("0.01"))
    except (ValueError, InvalidOperation):
        raise SourceError("SteamPy 市场金额无效") from None


def parse_page(payload: object, number: int, page_size: int) -> tuple[list[dict], int, int]:
    result = payload.get("result") if isinstance(payload, dict) and payload.get("success") is True else None
    if not isinstance(result, dict) or not isinstance(result.get("content"), list):
        raise SourceError("SteamPy 市场分页未返回成功结果")
    total = _integer(result.get("totalElements"), "市场总条数")
    pages = _integer(result.get("totalPages"), "市场总页数")
    if pages != (total + page_size - 1) // page_size or _integer(result.get("number"), "市场页码") != number - 1:
        raise SourceError("SteamPy 市场页码或总数不一致")
    rows = result["content"]
    expected = min(page_size, max(0, total - (number - 1) * page_size))
    if len(rows) != expected or any(not isinstance(row, dict) for row in rows):
        raise SourceError("SteamPy 市场页面条数不完整")
    return rows, total, pages


def summarize_asks(rows: list[dict], total: int, complete: bool) -> tuple[dict, tuple[Decimal, ...]]:
    seen, prices, stock = set(), [], 0
    near = []
    for row in rows:
        identifier = str(row.get("saleId") or "")
        if not identifier or identifier in seen:
            raise SourceError("SteamPy 在售列表缺少编号或跨页重复")
        seen.add(identifier)
        if row.get("ccy") != "CNY":
            raise SourceError("SteamPy 在售报价不是人民币国区列表")
        units = _integer(row.get("stock"), "在售库存")
        price = _price(row.get("keyPrice"))
        # The captured official query explicitly sorts the whole book ascending.
        if near and price < near[-1][0]:
            raise SourceError("SteamPy 在售报价未按低价排序，不能确认最低价")
        near.append((price, units))
        if units:
            prices.append(price)
            stock += units
    lowest = min(prices) if prices else None
    return {
        "ask_listings": total, "ask_sampled_listings": len(rows),
        "asks_complete": complete, "sampled_stock": stock,
        "stock": stock if complete else None,
        "lowest_ask": str(lowest) if lowest is not None else None,
        "near_lowest_stock": sum(units for price, units in near if lowest and price <= lowest * Decimal("1.05")),
    }, tuple(prices)


def summarize_requests(rows: list[dict], identity: ProductIdentity, total: int,
                       complete: bool, observed_at: str) -> dict:
    seen, active, recent = set(), [], 0
    observed = timestamp(observed_at)
    for row in rows:
        identifier = str(row.get("id") or "")
        if not identifier or identifier in seen:
            raise SourceError("SteamPy 求购列表缺少编号或跨页重复")
        seen.add(identifier)
        if str(row.get("gameId")) != identity.product_id:
            raise SourceError("SteamPy 求购单属于其他商品")
        names = [row.get(key) for key in ("gameName", "gameNameCn") if row.get(key)]
        if not names or any(not isinstance(name, str) or name.strip().casefold() not in
                            {value.strip().casefold() for value in identity.names} for name in names):
            raise SourceError("SteamPy 求购单名称与已核对详情不同")
        # The official '向他出售' control is enabled exactly for txStatus=02.
        if row.get("txStatus") != "02" or _integer(row.get("delFlag"), "求购有效标记") != 0:
            continue
        price = _price(row.get("txPrice"))
        try:
            created = timestamp(row["createTime"])
            if created > observed + timedelta(minutes=5):
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise SourceError("SteamPy 求购发布时间无效") from None
        active.append(price)
        recent += int(created >= observed - timedelta(days=7))
    return {
        "request_listings": total, "request_sampled_listings": len(rows),
        "requests_complete": complete, "open_requests": len(active),
        "recent_open_requests_7d": recent,
        "best_request_price": str(max(active)) if active else None,
    }


async def collect_public_demand(page, first_sale, identity: ProductIdentity, timeout_ms: int) -> tuple[dict, tuple[Decimal, ...]]:
    """Bounded reads of the exact product's national market; no trade endpoints.

    Reuse the actual page request's auth headers privately. Seller/buyer names,
    auth values and individual request/listing IDs never enter the snapshot.
    """
    observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    parts = urlsplit(first_sale.url)
    query = parse_qs(parts.query)
    if (identity.market_region != "cn" or parts.hostname != "steampy.com"
            or parts.path != "/xboot/steamKeySale/listSale"
            or query.get("gameId") != [identity.product_id]
            or query.get("pageNumber") != ["1"]
            or query.get("sort") != ["keyPrice"] or query.get("order") != ["asc"]):
        raise SourceError("SteamPy 市场查询未锁定已核对商品与低价排序")
    page_size = _integer((query.get("pageSize") or [None])[0], "市场每页条数")
    if not 1 <= page_size <= 50:
        raise SourceError("SteamPy 市场每页条数超出可核对范围")
    original = await first_sale.request.all_headers()
    headers = {key: value for key, value in original.items()
               if key not in {"host", "content-length", "cookie", "accept-encoding"}}

    async def get(path, parameters):
        try:
            response = await page.request.get("https://steampy.com/xboot" + path,
                                              params=parameters, headers=headers,
                                              timeout=min(timeout_ms, 4000))
            if not response.ok:
                raise SourceError(f"SteamPy 公开市场 HTTP {response.status}")
            return await response.json()
        except SourceError:
            raise
        except Exception:
            # Playwright exceptions can echo authentication headers.
            raise SourceError("SteamPy 公开市场读取超时或失败") from None

    async def book(path, parameters, size, first=None):
        async with asyncio.timeout(9):
            payload = await first.json() if first else await get(path, parameters)
            if first and not first.ok:
                raise SourceError(f"SteamPy 在售列表 HTTP {first.status}")
            rows, total, pages = parse_page(payload, 1, size)
            for number in range(2, min(pages, MAX_BOOK_PAGES) + 1):
                next_rows, next_total, next_pages = parse_page(await get(path, {**parameters, "pageNumber": number}), number, size)
                if (total, pages) != (next_total, next_pages):
                    raise SourceError("SteamPy 市场分页期间总数变化，本次证据不完整")
                rows.extend(next_rows)
            return rows, total, pages <= MAX_BOOK_PAGES

    sale_parameters = {key: value[0] for key, value in query.items()}
    requests_parameters = {"gameId": identity.product_id, "pageNumber": 1, "pageSize": 50,
                           "sort": "txPrice", "order": "desc"}
    # Both operations are read-only and independent, with their own deadlines.
    sale, wants = await asyncio.gather(
        book("/steamKeySale/listSale", sale_parameters, page_size, first_sale),
        book("/wantKeyOrder/showGame", requests_parameters, 50), return_exceptions=True)
    if isinstance(sale, BaseException):
        raise SourceError(str(sale) if isinstance(sale, SourceError) else "SteamPy 在售市场读取超时") from None
    asks, prices = summarize_asks(*sale)
    snapshot = {
        "model": "public_open_requests_and_supply_v1", "product_id": identity.product_id,
        "fingerprint": identity.fingerprint, "region": "cn", "observed_at": observed_at,
        "source": "https://steampy.com/cdkDetail?name=cn&gameId=" + identity.product_id,
        **asks, "requests_complete": False, "open_requests": None,
        "best_request_price": None, "recent_sales_7d": None, "recent_sales_30d": None,
        "estimated_sell_days": None,
        "sales_evidence": "未获取到按此商品和时间统计的可信成交记录",
    }
    try:
        if isinstance(wants, BaseException):
            raise SourceError(str(wants) if isinstance(wants, SourceError) else "SteamPy 求购读取超时")
        rows, total, complete = wants
        snapshot.update(summarize_requests(rows, identity, total, complete, observed_at))
    except SourceError as exc:
        snapshot["request_issue"] = str(exc)
    return snapshot, prices


def scoped_snapshot(snapshot: dict | None, identity: ProductIdentity | None,
                    now: datetime | None = None) -> bool:
    if not snapshot or not identity or not identity.detail_checked:
        return False
    if (snapshot.get("model") != "public_open_requests_and_supply_v1"
            or snapshot.get("product_id") != identity.product_id
            or snapshot.get("fingerprint") != identity.fingerprint
            or snapshot.get("region") != identity.market_region or identity.market_region != "cn"):
        return False
    try:
        age = ((now or datetime.now(timezone.utc)) - timestamp(snapshot["observed_at"])).total_seconds()
        return -5 <= age <= 300
    except (KeyError, ValueError, TypeError):
        return False


def market_trend(current: dict, history: list[dict]) -> dict:
    """Price/supply/request observations only; never infer completed trades."""
    now = timestamp(current["observed_at"])
    points = []
    seen_times = set()
    for row in history:
        if any(row.get(key) != current.get(key) for key in ("product_id", "fingerprint", "region", "model")):
            continue
        try:
            observed = timestamp(row["observed_at"])
        except (KeyError, TypeError, ValueError):
            continue
        if (MIN_TREND_SECONDS <= (now - observed).total_seconds() <= HISTORY_DAYS * 86400
                and observed not in seen_times):
            seen_times.add(observed)
            points.append(row)
    points.sort(key=lambda row: timestamp(row["observed_at"]))
    if not points:
        return {"status": "collecting", "observations": 1,
                "reason": "首轮观察；至少间隔 30 分钟再比较价格与需求", "samples": [trend_point(current)]}
    # Prefer a roughly one-day comparison; show the actual elapsed window.
    earlier = min(points, key=lambda row: abs((now - timestamp(row["observed_at"])).total_seconds() - 86400))
    def percent(field):
        before, after = earlier.get(field), current.get(field)
        if before is None or after is None or Decimal(str(before)) <= 0:
            return None
        return str(((Decimal(str(after)) / Decimal(str(before)) - 1) * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    stock_change = current["stock"] - earlier["stock"] if current.get("asks_complete") and earlier.get("asks_complete") else None
    request_change = (current["open_requests"] - earlier["open_requests"]
                      if current.get("requests_complete") and earlier.get("requests_complete") else None)
    samples = [*points, current]
    # Keep a representative, bounded timeline rather than just the last minutes.
    chosen = sorted({round(index * (len(samples) - 1) / min(11, len(samples) - 1)) for index in range(min(12, len(samples)))})
    return {"status": "observed", "observations": len(samples),
            "from": earlier["observed_at"], "to": current["observed_at"],
            "hours": round((now - timestamp(earlier["observed_at"])).total_seconds() / 3600, 1),
            "ask_change_pct": percent("lowest_ask"), "request_price_change_pct": percent("best_request_price"),
            "stock_change": stock_change, "open_requests_change": request_change,
            "samples": [trend_point(samples[index]) for index in chosen],
            "reason": "库存或求购单减少也可能是撤单，不能据此认定卖出或估计周转天数"}


def trend_point(row: dict) -> dict:
    return {key: row.get(key) for key in ("observed_at", "lowest_ask", "best_request_price", "stock", "open_requests",
                                         "asks_complete", "requests_complete")}
