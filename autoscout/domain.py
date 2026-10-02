"""Value objects and pure assessment rules."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import StrEnum
import re
from urllib.parse import urlparse

from .products import MarketProduct, ProductIdentity, ProductMatch, compare_products


CENT = Decimal("0.01")
PRICE_TOKEN = re.compile(r"(?<![\d.])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?(?![\d.])")


def money(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise ValueError("金额必须是有限数")
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def parse_price(text: str) -> Decimal:
    """Accept one displayed monetary number; reject ambiguous sale/original pairs."""
    matches = PRICE_TOKEN.findall(text)
    if len(matches) != 1:
        raise ValueError(f"无法从报价中确定唯一金额: {text!r}")
    try:
        value = money(Decimal(matches[0].replace(",", "")))
    except InvalidOperation as exc:
        raise ValueError(f"无效金额: {text!r}") from exc
    if value <= 0:
        raise ValueError("金额必须大于零")
    return value


def _https_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and bool(parsed.netloc)


@dataclass(frozen=True, slots=True)
class Offer:
    title: str
    url: str
    cost: Decimal
    alternate_titles: tuple[str, ...] = ()
    product: ProductIdentity | None = None

    def __post_init__(self) -> None:
        if (not self.title.strip() or not _https_url(self.url) or self.cost <= 0
                or any(not title.strip() for title in self.alternate_titles)):
            raise ValueError("商品需要名称、HTTPS 链接和正数价格")


@dataclass(frozen=True, slots=True)
class MarketQuote:
    title: str
    prices: tuple[Decimal, ...]
    url: str | None = None
    alternate_titles: tuple[str, ...] = ()
    product: ProductIdentity | None = None
    demand: dict | None = None

    def __post_init__(self) -> None:
        if not self.title.strip() or not self.prices or any(price <= 0 for price in self.prices):
            raise ValueError("市场报价需要名称和至少一个正数价格")
        if self.url is not None and not _https_url(self.url):
            raise ValueError("市场链接必须是 HTTPS")

    @property
    def lowest_price(self) -> Decimal:
        return min(self.prices)


@dataclass(frozen=True, slots=True)
class MarketLookup:
    quote: MarketQuote | None = None
    candidates: tuple[str, ...] = ()
    issue: str | None = None
    products: tuple[MarketProduct, ...] = ()


class Verdict(StrEnum):
    OPPORTUNITY = "opportunity"
    PRICE_ONLY = "price_only"
    LOW_MARGIN = "low_margin"
    NEEDS_REVIEW = "needs_review"
    NO_QUOTE = "no_quote"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class Assessment:
    offer: Offer
    verdict: Verdict
    reason: str
    quote: MarketQuote | None = None
    target_sell_price: Decimal | None = None
    net_profit: Decimal | None = None
    roi: Decimal | None = None
    pricing: dict[str, str] | None = None
    matching: dict | None = None
    market_candidates: tuple[dict, ...] = ()
    liquidity: dict | None = None


@dataclass(frozen=True, slots=True)
class PricingPolicy:
    fee_rate: Decimal = Decimal("0.03")
    min_profit: Decimal = Decimal("0.50")
    min_roi: Decimal = Decimal("0.05")
    undercut: Decimal = Decimal("0.01")
    payout_fee_rate: Decimal = Decimal("0.01")

    def __post_init__(self) -> None:
        values = (self.fee_rate, self.payout_fee_rate, self.min_profit, self.min_roi, self.undercut)
        if any(not value.is_finite() for value in values):
            raise ValueError("费率、利润门槛和压价幅度必须是有限数")
        if not Decimal("0") <= self.fee_rate < Decimal("1") or not Decimal("0") <= self.payout_fee_rate < Decimal("1"):
            raise ValueError("手续费率必须在 0 到 1 之间")
        if self.min_profit < 0 or self.min_roi < 0 or self.undercut < 0:
            raise ValueError("利润门槛和压价幅度不能为负数")


def project_cash(cost: Decimal, sell_price: Decimal,
                 policy: PricingPolicy) -> tuple[Decimal, Decimal, dict[str, str]]:
    """Estimate cash conversion when the withdrawal fee is debited in addition.

    The wallet must fund both the cash principal and its fee. Dividing wallet
    proceeds by (1 + payout rate) models that contract; adding fee percentages
    would incorrectly charge the payout fee on the gross selling price.
    Per-item withdrawal costs are estimates allocated to cents, not a promise
    that the platform supports a separate withdrawal for each item.
    """
    if not cost.is_finite() or not sell_price.is_finite() or cost <= 0 or sell_price <= 0:
        raise ValueError("进货成本和建议挂价必须是有限正数")
    sell_fee = money(sell_price * policy.fee_rate)
    wallet_credit = money(sell_price - sell_fee)
    cash_receipt = money(wallet_credit / (Decimal("1") + policy.payout_fee_rate))
    payout_fee = money(wallet_credit - cash_receipt)
    profit = money(cash_receipt - cost)
    roi = (profit / cost).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    pricing = {
        "model": "cash_principal_plus_withdrawal_fee_v1",
        "sell_fee_rate": str(policy.fee_rate), "payout_fee_rate": str(policy.payout_fee_rate),
        "min_profit": str(policy.min_profit), "min_roi": str(policy.min_roi),
        "undercut": str(policy.undercut), "estimated_sell_fee": str(sell_fee),
        "estimated_wallet_credit": str(wallet_credit), "estimated_payout_fee": str(payout_fee),
        "estimated_cash_receipt": str(cash_receipt),
    }
    return profit, roi, pricing


def profit_meets_thresholds(profit: Decimal, cost: Decimal, policy: PricingPolicy) -> bool:
    # Decide using the exact ratio; rounded display percentages must not admit
    # candidates that are actually below the configured ROI threshold.
    return profit >= policy.min_profit and profit >= cost * policy.min_roi


def assess_liquidity(offer: Offer, quote: MarketQuote, policy: PricingPolicy) -> dict:
    from .demand import scoped_snapshot
    snapshot = quote.demand
    if not scoped_snapshot(snapshot, quote.product):
        return {"status": "unverified", "reason": "尚无此商品的有效需求快照，挂价差不能证明卖得出去",
                "recent_sales_7d": None, "recent_sales_30d": None, "estimated_sell_days": None}
    result = dict(snapshot)
    count = snapshot.get("open_requests")
    if count is None:
        return {**result, "status": "unverified", "reason": snapshot.get("request_issue") or "求购需求未核实"}
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        return {**result, "status": "unverified", "reason": "有效求购条数无效，不能用于筛选"}
    bid = snapshot.get("best_request_price")
    if count > 0 and not bid:
        return {**result, "status": "unverified", "reason": "求购单缺少有效报价，不能用于筛选"}
    if count == 0:
        return {**result, "status": "no_open_requests" if snapshot.get("requests_complete") else "unverified",
                "reason": "当前公开求购列表中没有有效求购单；近期销量未知" if snapshot.get("requests_complete") else "求购列表不完整，不能判断无人求购"}
    try:
        profit, roi, pricing = project_cash(offer.cost, Decimal(bid), policy)
    except (ValueError, InvalidOperation):
        return {**result, "status": "unverified", "reason": "求购金额无效，不能用于筛选"}
    viable = profit_meets_thresholds(profit, offer.cost, policy)
    return {**result, "status": "request_viable" if viable else "request_below_threshold",
            "request_profit": str(profit), "request_roi": str(roi), "request_pricing": pricing,
            "reason": "当前有有效求购单，按最高求购价扣费后达到利润门槛；成交前仍需复核" if viable else
                      "当前求购价扣费后未达到利润门槛，挂价差尚无足够买方支持"}


def assess(offer: Offer, lookup: MarketLookup, catalog: "TitleCatalog", policy: PricingPolicy,
           mapping: dict | None = None) -> Assessment:
    candidates = tuple(product.snapshot() for product in lookup.products)
    if lookup.quote is None:
        evidence = ProductMatch(False, lookup.issue or "市场未确认对应商品").evidence(offer)
        if lookup.candidates:
            reason = lookup.issue or "存在候选商品，但无法确认同款"
            return Assessment(offer, Verdict.NEEDS_REVIEW,
                              f"{reason}；候选：{', '.join(lookup.candidates[:5])}",
                              matching=evidence, market_candidates=candidates)
        return Assessment(offer, Verdict.NO_QUOTE, lookup.issue or "市场未找到报价",
                          matching=evidence, market_candidates=candidates)

    market = MarketProduct(lookup.quote.title, lookup.quote.alternate_titles, lookup.quote.product)
    match = compare_products(offer, market, catalog, mapping)
    if lookup.quote.product and not lookup.quote.product.detail_checked:
        match = ProductMatch(False, "SteamPy 原商品详情尚未核对，不能据此计算利润", blocked=True)
    reason, evidence = match.reason, match.evidence(offer, market)
    if not match.accepted:
        return Assessment(offer, Verdict.NEEDS_REVIEW, reason, lookup.quote,
                          matching=evidence, market_candidates=candidates)

    price = money(lookup.quote.lowest_price - policy.undercut)
    if price <= 0:
        return Assessment(offer, Verdict.NEEDS_REVIEW, "市场最低价不足以设置有效卖价", lookup.quote,
                          matching=evidence, market_candidates=candidates)
    profit, roi, pricing = project_cash(offer.cost, price, policy)
    liquidity = assess_liquidity(offer, lookup.quote, policy)
    verdict = (Verdict.OPPORTUNITY if liquidity["status"] == "request_viable" else
               Verdict.PRICE_ONLY if profit_meets_thresholds(profit, offer.cost, policy) else Verdict.LOW_MARGIN)
    return Assessment(offer, verdict, reason + "；" + liquidity["reason"], lookup.quote, price, profit, roi,
                      pricing, evidence, candidates, liquidity)


from .titles import TitleCatalog  # noqa: E402 (type used in assess)
