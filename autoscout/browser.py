"""Read-only Playwright adapters for the two marketplaces.

Selectors are isolated here so a site change cannot alter accounting rules.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal, InvalidOperation
import json
import logging
import os
from pathlib import Path
import shutil
from urllib.parse import parse_qs, urlencode, urlsplit
import re

from playwright.async_api import TimeoutError as PlaywrightTimeout
from .domain import MarketLookup, MarketQuote, Offer, money, parse_price
from .demand import collect_public_demand
from .playwright_runtime import playwright_session
from .ports import FetchBatch, SourceError, Sources
from .products import MarketProduct, ProductIdentity, compare_products
from .session_restore import ensure_sonkwo_session, ensure_steampy_session
from .settings import Settings
from .titles import TitleCatalog, identity


log = logging.getLogger(__name__)
SONKWO_ORIGIN = "https://www.sonkwo.cn"
SONKWO_API = "https://api.sonkwo.cn/product/sku/page?locale=js"
STEAMPY_HOME = "https://steampy.com/home"


def browser_launch_options(playwright, settings: Settings, headless: bool) -> dict:
    """Find a usable Chromium installation, with an explicit channel override."""
    options = {"headless": headless}
    if settings.browser_channel == "auto":
        if Path(playwright.chromium.executable_path).is_file():
            return options
        candidates = [shutil.which("google-chrome"), shutil.which("chrome")]
        for root in (os.getenv("PROGRAMFILES"), os.getenv("PROGRAMFILES(X86)"),
                     os.getenv("LOCALAPPDATA")):
            if root:
                candidates.append(str(Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"))
        if any(path and Path(path).is_file() for path in candidates):
            options["channel"] = "chrome"
            return options
        raise SourceError("未找到 Chromium；运行 python -m playwright install chromium，或设置 AUTOSCOUT_BROWSER_CHANNEL")
    if settings.browser_channel:
        options["channel"] = settings.browser_channel
    return options


def parse_sonkwo_payload(payload: object, keyword: str, status: str) -> FetchBatch:
    """Validate current Sonkwo SKU JSON and retain only buyable China Steam keys."""
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise SourceError("杉果商品接口返回错误，请查看日志并稍后重试")
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("list"), list):
        raise SourceError("杉果商品接口格式已改变，需要更新采集器")
    expected_status = {"lowest": 1, "new_lowest": 2}[status]
    offers: list[Offer] = []
    warnings: list[str] = []
    needle = keyword.casefold().strip()
    for index, item in enumerate(data["list"]):
        if not isinstance(item, dict):
            warnings.append(f"杉果第 {index + 1} 项结构异常，已跳过")
            continue
        if (item.get("keyType") != 0 or item.get("region") != "cn"
                or item.get("priceStatus") != expected_status
                or item.get("supportCashPay") is not True
                or item.get("status") != 0 or item.get("onOffReason") != 1001):
            continue
        names = item.get("skuNames")
        if not isinstance(names, dict):
            warnings.append(f"杉果第 {index + 1} 项没有商品名称，已跳过")
            continue
        titles = tuple(dict.fromkeys(
            value.strip() for language in ("chs", "cht", "en", "default")
            if isinstance((value := names.get(language)), str) and value.strip()
        ))
        if not titles or (needle and not any(needle in title.casefold() for title in titles)):
            continue
        try:
            sku_id = int(item["id"])
            if sku_id <= 0:
                raise ValueError("商品编号无效")
            cost = money(Decimal(str(item["salePrice"])))
            if cost <= 0:
                raise ValueError("现金售价无效")
            url = f"https://www.sonkwo.hk/sku/{sku_id}"
            product = ProductIdentity("sonkwo", str(sku_id), titles, url,
                                      {0: "base", 1: "dlc"}.get(item.get("kind"), "unknown"), "cn",
                                      parent_product_id=str(item["productId"]) if item.get("productId") else None)
            offers.append(Offer(titles[0], url, cost, titles[1:], product))
        except (ValueError, KeyError, TypeError, InvalidOperation) as exc:
            warnings.append(f"杉果第 {index + 1} 项价格或编号异常，已跳过：{exc}")
    if not keyword and data["list"] and not offers:
        warnings.append("杉果返回了商品，但整页都不符合可购买国区 Steam Key 条件；请核对接口筛选")
    return FetchBatch(tuple(offers), tuple(warnings))


def parse_sonkwo_product_page(offer: Offer, html: str) -> Offer:
    """Read public loader JSON as data, using the original SKU's domain/namespace."""
    if not offer.product:
        raise SourceError("杉果商品缺少编号快照")
    marker = "window.__remixContext = "
    try:
        state, _ = json.JSONDecoder().raw_decode(html[html.index(marker) + len(marker):])
        loader = state["state"]["loaderData"]["routes/__pc/__pages/sku/$id"]
        skus = loader["skus"]
        sku = next(item for item in skus if str(item.get("id")) == offer.product.product_id)
    except (ValueError, TypeError, KeyError, StopIteration) as exc:
        raise SourceError("杉果详情格式改变或商品编号未找到") from exc
    if str(loader.get("current_sku")) != offer.product.product_id:
        raise SourceError("杉果详情跳到了其他商品编号")
    parent = str(sku.get("product_id", ""))
    if offer.product.parent_product_id and parent != offer.product.parent_product_id:
        raise SourceError("杉果详情的所属产品编号与目录不同，可能混用了域名或商品来源")
    if sku.get("region") != "cn" or sku.get("key_type") != "steam_key":
        raise SourceError("杉果详情不属于国区 Steam Key")
    names = [sku.get("sku_name"), sku.get("sku_ename")]
    names.extend((sku.get("sku_names") or {}).values())
    detail_names = tuple(dict.fromkeys(name.strip() for name in names if isinstance(name, str) and name.strip()))
    if not detail_names or not any(identity(name)[0] == identity(old)[0]
                                   and identity(name)[1:] == identity(old)[1:]
                                   for name in detail_names for old in offer.product.names):
        raise SourceError("杉果详情名称与目录名称不一致")
    titles = tuple(dict.fromkeys((offer.title, *offer.alternate_titles, *detail_names)))
    kind = {"Products::Base": "base", "Products::Dlc": "dlc", "Products::DLC": "dlc",
            "Products::Bundle": "bundle"}.get(loader.get("type"), "unknown")
    if kind == "unknown":
        raise SourceError("杉果详情内容类型未识别，需要更新详情采集器")
    if (offer.product.content_type != "unknown" and kind != offer.product.content_type):
        raise SourceError("杉果详情内容类型与目录不同")
    # Do not infer an AppID from product IDs, images or generic Steam links.
    product = replace(offer.product, names=titles, content_type=kind, detail_checked=True)
    return replace(offer, alternate_titles=titles[1:], product=product)


class SonkwoOffers:
    def __init__(self, page, timeout_ms: int, profile: Path):
        self.page = page
        self.timeout_ms = timeout_ms
        self.profile = profile
        self._login_checked = False

    async def list_offers(self, keyword: str, page: int, status: str) -> FetchBatch:
        if status not in {"lowest", "new_lowest"} or page < 1:
            raise ValueError("不支持的杉果搜索参数")
        if not self._login_checked:
            if not await ensure_sonkwo_session(self.page.context, self.profile, self.timeout_ms):
                raise SourceError("杉果续期凭证已失效；打开 /auth 点击杉果的“开始登录”")
            try:
                await self.page.goto(f"{SONKWO_ORIGIN}/store/search",
                                     wait_until="domcontentloaded", timeout=self.timeout_ms)
                await self.page.locator("input.SK-header-search-text").wait_for(
                    state="visible", timeout=self.timeout_ms)
            except PlaywrightTimeout as exc:
                raise SourceError("杉果页面加载超时；请检查网络") from exc
            self._login_checked = True
        body = {
            "sonkwo_version": 1, "sonkwo_client": "web", "regions": ["cn"],
            "per": 20, "page": page, "cates": ["game"],
            "keyTypes": [0], "priceStatus": {"lowest": 1, "new_lowest": 2}[status],
            "tagIds": [],
        }
        if keyword:
            body.update({"statuses": [0, 1, 3], "searchWord": keyword,
                         "onOffReasons": [1001, 2004, 3001]})
        else:
            body.update({"supportCashPay": True, "statuses": [0],
                         "salePriceMin": 0.01, "createdAtSort": 1,
                         "allSearchScoreSort": 1, "onOffReasons": [1001]})
        try:
            response = await self.page.request.post(SONKWO_API, data=body, timeout=self.timeout_ms)
            if not response.ok:
                raise SourceError(f"杉果商品接口 HTTP {response.status}")
            payload = json.loads((await response.body()).decode("utf-8"))
        except PlaywrightTimeout as exc:
            raise SourceError(f"杉果商品接口超时（第 {page} 页）") from exc
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise SourceError("杉果商品接口返回无法解析的数据") from exc
        return parse_sonkwo_payload(payload, keyword, status)

    async def verify_offer(self, offer: Offer) -> Offer:
        try:
            response = await self.page.request.get(offer.url, timeout=self.timeout_ms)
            if not response.ok or response.url != offer.url:
                raise SourceError(f"杉果原商品详情不可用：HTTP {response.status}")
            return parse_sonkwo_product_page(offer, await response.text())
        except PlaywrightTimeout as exc:
            raise SourceError("杉果商品详情核对超时") from exc


def parse_steampy_search_payload(payload: object) -> tuple[str, ...]:
    """Read the displayed game names from the current keyByName response."""
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise SourceError("SteamPy 搜索接口返回错误")
    result = payload.get("result")
    content = result.get("content") if isinstance(result, dict) else None
    if not isinstance(content, list):
        raise SourceError("SteamPy 搜索接口格式已改变")
    names = []
    for item in content:
        if not isinstance(item, dict):
            raise SourceError("SteamPy 搜索结果结构异常")
        name = (item.get("gameNameCn") or item.get("gameName") or "")
        if not isinstance(name, str) or not name.strip():
            raise SourceError("SteamPy 搜索结果缺少游戏名称")
        names.append(name.strip())
    return tuple(names)


def parse_steampy_product(item: dict, *, detail_checked: bool = False) -> MarketProduct:
    names = tuple(dict.fromkeys(value.strip() for key in ("gameNameCn", "gameName")
                               if isinstance((value := item.get(key)), str) and value.strip()))
    product_id = str(item.get("id", ""))
    if not names or not product_id.isdigit() or int(product_id) <= 0:
        raise SourceError("SteamPy 商品缺少编号或官方名称")
    app_id = str(item.get("appId") or "")
    if not app_id.isdigit() or int(app_id) <= 0:
        app_id = None
    steam_app = item.get("steamApp") or {}
    if not isinstance(steam_app, dict):
        raise SourceError("SteamPy Steam 产品信息结构异常")
    nested_id = str(steam_app.get("appId") or "")
    game_url = item.get("gameUrl") or ""
    parsed = urlsplit(game_url) if isinstance(game_url, str) else urlsplit("")
    linked_id = re.match(r"^/app/(\d+)(?:/|$)", parsed.path) if parsed.hostname == "store.steampowered.com" else None
    if app_id and ((nested_id.isdigit() and nested_id != app_id)
                   or (linked_id and linked_id[1] != app_id)):
        raise SourceError("SteamPy 商品的 Steam AppID 字段相互冲突")
    kind = {"game": "base", "dlc": "dlc", "music": "soundtrack"}.get(steam_app.get("type"), "unknown")
    url = "https://steampy.com/cdkDetail?" + urlencode({"name": "cn", "gameId": product_id})
    # bundleId=000000 and gamePath have no verified package-ID contract.
    product = ProductIdentity("steampy", product_id, names, url, kind, "cn",
                              steam_app_id=app_id, detail_checked=detail_checked)
    return MarketProduct(names[0], names[1:], product)


def parse_steampy_products(payload: object) -> tuple[MarketProduct, ...]:
    parse_steampy_search_payload(payload)
    return tuple(parse_steampy_product(item) for item in payload["result"]["content"])


class SteamPyQuotes:
    def __init__(self, page, timeout_ms: int, catalog: TitleCatalog, mapping_loader=None):
        self.page = page
        self.timeout_ms = timeout_ms
        self.catalog = catalog
        self.mapping_loader = mapping_loader
        self._current_products: tuple[MarketProduct, ...] = ()
        self._search_has_more = False

    async def lookup(self, offer: Offer) -> MarketLookup:
        mapping = self.mapping_loader(offer.product.product_id) if self.mapping_loader and offer.product else None
        terms = list(self.catalog.offer_search_terms(offer))
        if mapping:
            terms.extend(TitleCatalog().search_terms(name)[0] for name in mapping["market"]["names"]
                         if TitleCatalog().search_terms(name))
        products: dict[str, MarketProduct] = {}
        incomplete = False
        await self._open_market()
        for term in dict.fromkeys(terms):
            await self._search_market(term)
            incomplete |= self._search_has_more
            for product in self._current_products:
                previous = products.get(product.identity.product_id)
                if previous and previous.identity.fingerprint != product.identity.fingerprint:
                    raise SourceError("SteamPy 同一商品在不同搜索中的信息不一致")
                products[product.identity.product_id] = product
        candidates = tuple(product.title for product in products.values())
        all_products = tuple(products.values())
        matches = [product for product in all_products
                   if compare_products(offer, product, self.catalog, mapping).accepted]
        if len(matches) != 1 or incomplete:
            issue = ("搜索结果有多页，当前候选不完整；请缩小关键词后核对" if incomplete else
                     "多个对应市场商品，需要确认具体商品编号" if len(matches) > 1 else
                     "尚未确认对应商品，请查看两边官方名称、版本与内容")
            return MarketLookup(candidates=candidates, issue=issue if candidates else None, products=all_products)
        product = matches[0]
        # Navigate by the captured product ID, then verify getOne before reading
        # a price. A click/card index alone cannot establish product identity.
        def is_detail(response):
            return urlsplit(response.url).path.endswith("/steamGame/getOne")
        def is_sale(response):
            url = urlsplit(response.url)
            return (url.path == "/xboot/steamKeySale/listSale"
                    and parse_qs(url.query).get("gameId") == [product.identity.product_id]
                    and parse_qs(url.query).get("pageNumber") == ["1"])
        try:
            async with self.page.expect_response(is_detail, timeout=self.timeout_ms) as pending, \
                    self.page.expect_response(is_sale, timeout=self.timeout_ms) as sale_pending:
                await self.page.goto(product.identity.url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            response = await pending.value
            if not response.ok:
                raise SourceError(f"SteamPy 商品详情 HTTP {response.status}")
            payload = await response.json()
            if payload.get("success") is not True or not isinstance(payload.get("result"), dict):
                raise SourceError("SteamPy 商品详情结构已改变")
            detail = parse_steampy_product(payload["result"], detail_checked=True)
            if detail.identity.fingerprint != product.identity.fingerprint:
                raise SourceError("SteamPy 详情编号、名称或内容与搜索结果不同")
            current = urlsplit(self.page.url)
            if (current.hostname != "steampy.com" or current.path != "/cdkDetail"
                    or parse_qs(current.query).get("gameId") != [product.identity.product_id]
                    or parse_qs(current.query).get("name") != ["cn"]):
                raise SourceError("SteamPy 报价页面跳到了其他商品或市场区域")
        except PlaywrightTimeout as exc:
            raise SourceError(f"SteamPy 商品详情或报价表加载超时：{product.title}") from exc
        demand, prices = await collect_public_demand(self.page, await sale_pending.value, detail.identity, self.timeout_ms)
        if not prices:
            return MarketLookup(candidates=(detail.title,), issue="同款商品没有可确认库存的在售报价", products=all_products)
        return MarketLookup(MarketQuote(detail.title, tuple(prices), detail.identity.url,
                                       detail.alternate_titles, detail.identity, demand), candidates, products=all_products)

    async def _search_market(self, term: str) -> tuple[str, ...]:
        """Wait for this query's response and matching cards, not old visible cards."""
        def is_current_search(response) -> bool:
            url = urlsplit(response.url)
            return (url.path.endswith("/steamGame/keyByName")
                    and parse_qs(url.query).get("gameName") == [term])

        try:
            async with self.page.expect_response(is_current_search, timeout=self.timeout_ms) as pending:
                search_box = self.page.locator(".ivu-input").first
                await search_box.fill(term)
                await search_box.press("Enter")
            response = await pending.value
            if not response.ok:
                raise SourceError(f"SteamPy 搜索接口 HTTP {response.status}：{term}")
            payload = await response.json()
            names = parse_steampy_search_payload(payload)
            self._current_products = parse_steampy_products(payload)
            self._search_has_more = int(payload["result"].get("totalPages") or 1) > 1
            if names:
                await self.page.wait_for_function("""expected => {
                    const shown = Array.from(document.querySelectorAll('.gameblock .gameName'))
                        .map(node => node.textContent.trim());
                    return shown.length === expected.length
                        && shown.every((name, index) => name.length > 0
                            && expected[index].startsWith(name));
                }""", arg=names, timeout=self.timeout_ms)
            return names
        except PlaywrightTimeout as exc:
            raise SourceError(f"SteamPy 搜索结果未及时更新：{term}") from exc


    async def _open_market(self) -> None:
        try:
            await self.page.goto(STEAMPY_HOME, wait_until="domcontentloaded", timeout=self.timeout_ms)
            await self.page.locator(
                "li:has-text('退出登录'), .ivu-menu-submenu:has-text('卖家中心')"
            ).first.wait_for(state="visible", timeout=self.timeout_ms)
            menu = self.page.locator("li.ivu-menu-submenu:has-text('CDKey市场')").first
            await menu.wait_for(state="visible", timeout=self.timeout_ms)
            item = self.page.locator("li.ivu-menu-item:has-text('CDKey市场-国区')").first
            if not await item.is_visible():
                await menu.click()
            await item.click(timeout=self.timeout_ms)
            await self.page.locator(".ivu-input").first.wait_for(state="visible", timeout=self.timeout_ms)
        except PlaywrightTimeout as exc:
            raise SourceError("SteamPy 市场不可用或登录已过期；打开 /auth 点击 SteamPy 的“开始登录”") from exc


@asynccontextmanager
async def browser_sources(settings: Settings, catalog: TitleCatalog, mapping_loader=None):
    """Each scan owns and closes both browser profiles, even after cancellation."""
    for platform in ("sonkwo", "steampy"):
        settings.profile_path(platform).parent.mkdir(parents=True, exist_ok=True)
    async with playwright_session(settings.data_dir) as playwright:
        launch_options = browser_launch_options(playwright, settings, settings.headless)
        sonkwo_context = await playwright.chromium.launch_persistent_context(
            str(settings.profile_path("sonkwo")), **launch_options
        )
        try:
            steampy_context = await playwright.chromium.launch_persistent_context(
                str(settings.profile_path("steampy")), **launch_options
            )
            try:
                sonkwo_page = sonkwo_context.pages[0] if sonkwo_context.pages else await sonkwo_context.new_page()
                steampy_page = steampy_context.pages[0] if steampy_context.pages else await steampy_context.new_page()
                timeout_ms = int(settings.operation_timeout * 1000)
                if not await ensure_steampy_session(
                        steampy_context, steampy_page,
                        settings.profile_path("steampy"), timeout_ms):
                    raise SourceError("SteamPy 账号登录已过期；打开 /auth 点击 SteamPy 的“开始登录”")
                yield Sources(
                    SonkwoOffers(sonkwo_page, timeout_ms, settings.profile_path("sonkwo")),
                    SteamPyQuotes(steampy_page, timeout_ms, catalog, mapping_loader),
                )
            finally:
                await steampy_context.close()
        finally:
            await sonkwo_context.close()
