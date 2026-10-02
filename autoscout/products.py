"""Product snapshots and explicit evidence for discovery matching.

Internal marketplace identifiers are never treated as Steam identifiers. Prices,
timestamps and descriptions do not participate in a confirmed mapping's identity.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from .titles import TitleCatalog, identity

if TYPE_CHECKING:
    from .domain import Offer


@dataclass(frozen=True, slots=True)
class ProductIdentity:
    platform: str
    product_id: str
    names: tuple[str, ...]
    url: str
    content_type: str = "unknown"
    market_region: str = ""
    steam_app_id: str | None = None
    steam_package_id: str | None = None
    detail_checked: bool = False
    detail_issue: str | None = None
    parent_product_id: str | None = None

    def __post_init__(self) -> None:
        if (self.platform not in {"sonkwo", "steampy"} or not self.product_id.isdigit()
                or int(self.product_id) <= 0 or not self.names
                or any(not name.strip() for name in self.names)
                or urlsplit(self.url).scheme != "https"):
            raise ValueError("商品身份快照缺少平台、编号、官方名称或 HTTPS 链接")
        for value in (self.steam_app_id, self.steam_package_id):
            if value is not None and (not value.isdigit() or int(value) <= 0):
                raise ValueError("Steam 标识必须来自有效的官方产品字段")

    @property
    def fingerprint(self) -> str:
        stable = {key: value for key, value in asdict(self).items()
                  if key not in {"detail_checked", "detail_issue"}}
        # Translation ordering and capitalization alone do not change a product.
        stable["names"] = sorted({name.strip().casefold() for name in self.names})
        stable["editions"] = sorted({identity(name)[1] for name in self.names})
        return hashlib.sha256(json.dumps(stable, sort_keys=True, ensure_ascii=False)
                              .encode("utf-8")).hexdigest()

    def snapshot(self) -> dict:
        return {**asdict(self), "fingerprint": self.fingerprint,
                "editions": sorted({identity(name)[1] for name in self.names})}

    @classmethod
    def from_snapshot(cls, value: dict) -> "ProductIdentity":
        fields = {key: value.get(key) for key in cls.__dataclass_fields__ if key in value}
        fields["names"] = tuple(fields.get("names", ()))
        return cls(**fields)


@dataclass(frozen=True, slots=True)
class MarketProduct:
    title: str
    alternate_titles: tuple[str, ...] = ()
    identity: ProductIdentity | None = None

    def snapshot(self) -> dict:
        return {"title": self.title, "alternate_titles": self.alternate_titles,
                "identity": self.identity.snapshot() if self.identity else None}


@dataclass(frozen=True, slots=True)
class ProductMatch:
    accepted: bool
    reason: str
    method: str = "unconfirmed"
    offer_name: str | None = None
    market_name: str | None = None
    blocked: bool = False
    confirmed_at: str | None = None

    def evidence(self, offer: "Offer", market: MarketProduct | None = None) -> dict:
        return {**asdict(self), "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "offer": offer.product.snapshot() if offer.product else None,
                "market": market.identity.snapshot() if market and market.identity else None,
                "limitation": "AppID 仅标识应用，不能独立证明版本或激活包内容；国区市场分类不等于逐 Key 激活区域保证"}


def product_guard(offer: "Offer", market: MarketProduct) -> str | None:
    """Hard conflicts cannot be overridden by an alias or a saved confirmation."""
    left, right = offer.product, market.identity
    for label, product in (("杉果", left), ("SteamPy", right)):
        if product and product.detail_issue:
            return f"{label}详情核对失败：{product.detail_issue}"
    if left and not left.detail_checked:
        return "杉果原商品详情尚未核对编号与名称"
    offer_names = (offer.title, *offer.alternate_titles)
    market_names = (market.title, *market.alternate_titles)
    left_editions = {identity(name)[1] for name in offer_names}
    right_editions = {identity(name)[1] for name in market_names}
    if len(left_editions) != 1 or "mixed" in left_editions:
        return "杉果官方名称的版本信息相互冲突"
    if len(right_editions) != 1 or "mixed" in right_editions:
        return "SteamPy 官方名称的版本信息相互冲突"
    if left_editions != right_editions:
        return f"版本不一致：{' / '.join(left_editions)} / {' / '.join(right_editions)}"
    if left and right:
        if (left.steam_app_id and right.steam_app_id
                and left.steam_app_id != right.steam_app_id):
            return "两边 Steam AppID 不同"
        if (left.steam_package_id and right.steam_package_id
                and left.steam_package_id != right.steam_package_id):
            return "两边 Steam 激活包编号不同"
        if (left.content_type != "unknown" and right.content_type != "unknown"
                and left.content_type != right.content_type):
            return f"商品内容类型不同：{left.content_type} / {right.content_type}"
        if (left.market_region and right.market_region
                and left.market_region != right.market_region):
            return "两边市场区域分类不同"
    return None


def compare_products(offer: "Offer", market: MarketProduct, catalog: TitleCatalog,
                     mapping: dict | None = None) -> ProductMatch:
    conflict = product_guard(offer, market)
    if conflict:
        return ProductMatch(False, conflict, blocked=True)
    left, right = offer.product, market.identity
    if mapping and left and right:
        if (mapping["sonkwo_id"] == left.product_id and mapping["steampy_id"] == right.product_id):
            if (mapping["offer_fingerprint"] != left.fingerprint
                    or mapping["market_fingerprint"] != right.fingerprint):
                return ProductMatch(False, "已确认商品的名称、版本或内容信息已变化，需要重新确认")
            return ProductMatch(True, "已按确认的两个商品编号匹配；当前商品信息未变", "confirmed_product_pair",
                                confirmed_at=mapping["confirmed_at"])
        if mapping["sonkwo_id"] == left.product_id:
            return ProductMatch(False, "此杉果商品已对应其他 SteamPy 商品编号")

    if left and right and "unknown" in {left.content_type, right.content_type}:
        return ProductMatch(False, "平台未提供可识别的商品内容类型，需要确认具体商品内容")

    # Special content retains its complete variant subtitle and requires a
    # product-level confirmation rather than stripping a DLC/bundle marker.
    if (any(identity(name)[2] for name in (offer.title, *offer.alternate_titles,
                                         market.title, *market.alternate_titles))
            or (left and left.content_type not in {"base", "unknown"})
            or (right and right.content_type not in {"base", "unknown"})):
        return ProductMatch(False, "DLC、原声、合集或升级内容需要确认具体商品内容")

    for offer_name in (offer.title, *offer.alternate_titles):
        for market_name in (market.title, *market.alternate_titles):
            matched, reason = catalog.compare(offer_name, market_name)
            if matched:
                method = "official_names" if identity(offer_name)[0] == identity(market_name)[0] else "configured_alias"
                return ProductMatch(True, f"官方多语言名称核对：{offer_name} ↔ {market_name}；{reason}",
                                    method, offer_name, market_name)
    if left and right:
        if (left.steam_package_id and left.steam_package_id == right.steam_package_id
                and left.steam_app_id and left.steam_app_id == right.steam_app_id):
            return ProductMatch(True, "Steam 应用与激活包编号相同，版本无冲突", "steam_package")
        if (left.steam_app_id and left.steam_app_id == right.steam_app_id
                and identity(offer.title)[1] == "standard"
                and left.content_type == right.content_type == "base"):
            return ProductMatch(False, "Steam AppID 相同；完整名称不同，仍需确认版本与激活包内容", "steam_app_candidate")
    return ProductMatch(False, "官方名称尚未对应；名称相似只能作为候选，需要确认具体商品")
