"""Read-only import of existing Sonkwo purchases and SteamPy CDKey sales.

Only whitelisted order metadata is retained. Platform responses can contain
CD keys and account details, so raw payloads must never be logged or saved.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import time
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from .browser import browser_launch_options
from .domain import money
from .playwright_runtime import playwright_session
from .ports import SourceError
from .repository import Repository
from .session_restore import ensure_sonkwo_session, ensure_steampy_session
from .settings import Settings
from .titles import TitleCatalog


SONKWO_ORDERS = "https://www.sonkwo.cn/setting/orders"
STEAMPY_HOME = "https://steampy.com/home"
MAX_ORDER_PAGES = 200
SONKWO_ITEM_STATES = {"0": "created", "1": "completed", "2": "refunded", "3": "refunding"}


@dataclass(frozen=True, slots=True)
class PurchaseLine:
    line_no: int
    title: str
    alternate_titles: tuple[str, ...]
    quantity: int
    unit_cost: Decimal
    refunded_quantity: int = 0
    refund_amount: Decimal = Decimal("0")
    refund_source: str = ""
    product_id: str | None = None


@dataclass(frozen=True, slots=True)
class AccountOrder:
    platform: str
    order_id: str
    title: str
    occurred_at: str
    status: str
    quantity: int
    amount: Decimal | None
    fee: Decimal | None = None
    net_amount: Decimal | None = None
    alternate_titles: tuple[str, ...] = ()
    lines: tuple[PurchaseLine, ...] = ()
    channel: str = "ordinary"
    product_id: str | None = None
    stock_created_at: str | None = None


def _product_id(value: object) -> str | None:
    if value is None or value == "":
        return None
    result = str(value)
    if not result.isdigit() or int(result) <= 0 or len(result) > 64:
        raise SourceError("订单商品编号无效")
    return result


def _steampy_product_id(row: dict, game: dict) -> str | None:
    identifiers = {_product_id(value) for value in (row.get("gameId"), game.get("id")) if value is not None}
    identifiers.discard(None)
    if len(identifiers) > 1:
        raise SourceError("SteamPy 订单商品编号与商品详情不一致")
    return next(iter(identifiers), None)


def _decimal(value: object, field: str) -> Decimal:
    try:
        result = money(Decimal(str(value)))
    except Exception as exc:
        raise SourceError(f"订单金额字段无效：{field}") from exc
    if result < 0:
        raise SourceError(f"订单金额字段为负：{field}")
    return result


def parse_sonkwo_orders(rows: list[dict]) -> list[AccountOrder]:
    orders = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("id") or not isinstance(row.get("subOrders"), list):
            raise SourceError("杉果订单结构已改变")
        lines = row["subOrders"]
        if not lines:
            raise SourceError("杉果订单缺少商品明细")
        state = str(row.get("state") or "unknown")
        quantity = 0
        titles = []
        line_paid = Decimal("0")
        parsed_lines: list[PurchaseLine] = []
        for line_no, line in enumerate(lines, start=1):
            if not isinstance(line, dict) or not isinstance(line.get("sku"), dict):
                raise SourceError("杉果订单商品结构已改变")
            try:
                count = int(line["quantity"])
            except (KeyError, TypeError, ValueError) as exc:
                raise SourceError("杉果订单商品数量无效") from exc
            if count <= 0:
                raise SourceError("杉果订单商品数量无效")
            quantity += count
            names = tuple(dict.fromkeys(line["sku"][key].strip()
                                            for key in ("chsName", "enName", "chtName")
                                            if isinstance(line["sku"].get(key), str)
                                            and line["sku"][key].strip()))
            title = names[0] if names else "未知商品"
            titles.append(title)
            unit_cost = _decimal(line.get("realPrice"), "杉果商品实付")
            line_paid += unit_cost * count
            # Raw order/page uses 0/1/2/3; the official UI normalizes them to
            # created/completed/refunded/refunding before rendering. Preserve
            # unknown or missing states as an import error rather than silently
            # counting possibly refunded items as new inventory.
            raw_state = line.get("status")
            item_state = SONKWO_ITEM_STATES.get(str(raw_state), str(raw_state))
            if state != "refunded" and item_state not in SONKWO_ITEM_STATES.values():
                raise SourceError("杉果订单商品退款状态缺失或未知，已停止同步")
            refunded = item_state == "refunded" or state == "refunded"
            parsed_lines.append(PurchaseLine(
                line_no, title, names[1:], count, unit_cost,
                count if refunded else 0,
                money(unit_cost * count) if refunded else Decimal("0"),
                "platform" if refunded else "",
                _product_id(line["sku"].get("id")),
            ))
        subtotal = _decimal(row.get("subtotal"), "杉果订单实付")
        if state == "completed" and abs(subtotal - line_paid) > Decimal("0.01"):
            raise SourceError("杉果订单合计与商品实付不一致，已停止同步")
        timestamp = row.get("createdAt")
        if not isinstance(timestamp, (int, float)):
            raise SourceError("杉果订单时间格式已改变")
        occurred_at = datetime.fromtimestamp(timestamp / 1000, timezone.utc).isoformat(timespec="seconds")
        unique_titles = list(dict.fromkeys(titles))
        summary_title = unique_titles[0] if len(unique_titles) == 1 else f"{unique_titles[0]} 等 {quantity} 件"
        orders.append(AccountOrder("sonkwo", str(row["id"]), summary_title, occurred_at,
                                   state, quantity, subtotal, lines=tuple(parsed_lines)))
    return orders


def parse_steampy_orders(normal: list[dict], successful: list[dict]) -> list[AccountOrder]:
    success_by_id = {}
    for row in successful:
        if not isinstance(row, dict) or not row.get("id"):
            raise SourceError("SteamPy 成功订单结构已改变")
        order_id = str(row["id"])
        if order_id in success_by_id:
            raise SourceError("SteamPy 成功订单编号重复")
        success_by_id[order_id] = row
    normal_ids = {str(row.get("id")) for row in normal if isinstance(row, dict)}
    if not set(success_by_id).issubset(normal_ids):
        raise SourceError("SteamPy 成功订单与订单总列表不一致")
    orders = []
    for row in normal:
        if not isinstance(row, dict) or not row.get("id"):
            raise SourceError("SteamPy 订单结构已改变")
        order_id = str(row["id"])
        success = success_by_id.get(order_id)
        details = success or row
        game = details.get("steamGame") if isinstance(details.get("steamGame"), dict) else {}
        names = tuple(dict.fromkeys(value.strip() for value in (
            game.get("gameNameCn"), game.get("gameName"),
            row.get("gameNameCn"), row.get("gameName"))
            if isinstance(value, str) and value.strip()))
        title = names[0] if names else "未知商品"
        product_id = _steampy_product_id(details, game)
        normal_id = _product_id(row.get("gameId"))
        if product_id and normal_id and product_id != normal_id:
            raise SourceError("SteamPy 成功订单与普通订单的商品编号不同")
        product_id = product_id or normal_id
        occurred_at = str(details.get("txTime") or details.get("createTime") or "")
        if not occurred_at:
            raise SourceError("SteamPy 订单缺少时间")
        if success and not success.get("refundTime"):
            gross = _decimal(success.get("txPrice"), "SteamPy 成交价")
            fee = _decimal(success.get("fee"), "SteamPy 手续费")
            tech_fee = _decimal(success.get("techFee") or 0, "SteamPy 技术服务费")
            extra_fee = success.get("osFee")
            if extra_fee is not None and _decimal(extra_fee, "SteamPy 额外费用") != 0:
                raise SourceError("SteamPy 订单出现额外费用字段，需先核对")
            fee += tech_fee
            if fee > gross:
                raise SourceError("SteamPy 订单费用高于成交价")
            orders.append(AccountOrder("steampy", order_id, title, occurred_at,
                                       "sold", 1, gross, fee, money(gross - fee), names[1:],
                                       product_id=product_id,
                                       stock_created_at=(success.get("steamKeySaleDetail") or {}).get("createTime")
                                       if isinstance(success.get("steamKeySaleDetail"), dict) and success.get("txTime") else None))
        else:
            status = "refunded" if success else f"status_{row.get('txStatus', 'unknown')}"
            orders.append(AccountOrder("steampy", order_id, title, occurred_at,
                                       status, 1, None, alternate_titles=names[1:], product_id=product_id))
    return orders


def parse_steampy_request_orders(rows: list[dict]) -> list[AccountOrder]:
    """Import the seller's fulfilled buyer requests without guessing their gross fee.

The platform's txPrice matches the account's AK wallet credit on the live
fixture. Its oriPrice has a different value, but that difference is not
labelled as a seller fee in this response, so only net income is recorded.
    """
    orders = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or not row.get("id"):
            raise SourceError("SteamPy 求购订单结构已改变")
        order_id = str(row["id"])
        if order_id in seen:
            raise SourceError("SteamPy 求购订单编号重复")
        seen.add(order_id)
        names = tuple(dict.fromkeys(
            value.strip() for value in (row.get("gameNameCn"), row.get("gameName"))
            if isinstance(value, str) and value.strip()))
        created_at = row.get("createTime")
        if not isinstance(created_at, str) or not created_at:
            raise SourceError("SteamPy 求购订单缺少时间")
        status = str(row.get("txStatus") or "unknown")
        if status == "20":
            # createTime is when the buyer posted the request. On verified live
            # sales, updateTime tracks fulfillment and agrees with the AK wallet
            # credit; using creation time can reject a purchase made in between.
            occurred_at = row.get("updateTime")
            if not isinstance(occurred_at, str) or not occurred_at:
                raise SourceError("SteamPy 求购成交缺少完成时间，不能按求购创建时间配对")
            try:
                if datetime.fromisoformat(occurred_at) < datetime.fromisoformat(created_at):
                    raise ValueError("完成时间早于创建时间")
            except ValueError as exc:
                raise SourceError("SteamPy 求购成交时间无效") from exc
            net = _decimal(row.get("txPrice"), "SteamPy 求购扣费后收入")
            orders.append(AccountOrder(
                "steampy", order_id, names[0] if names else "未知商品",
                occurred_at, "sold", 1, None, None, net, names[1:],
                channel="request", product_id=_product_id(row.get("gameId"))))
        else:
            orders.append(AccountOrder(
                "steampy", order_id, names[0] if names else "未知商品",
                created_at, f"status_{status}", 1, None,
                alternate_titles=names[1:], channel="request", product_id=_product_id(row.get("gameId"))))
    return orders


def _page_url(url: str, field: str, number: int) -> str:
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    query[field] = [str(number)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                      urlencode(query, doseq=True), ""))


async def _all_pages(page, first, *, field: str, count: int, data_key: str,
                     list_key: str, expected_path: str, progress: Callable[[int, int], None],
                     timeout_ms: int) -> list[dict]:
    if not 0 <= count <= MAX_ORDER_PAGES or urlsplit(first.url).path != expected_path:
        raise SourceError("订单分页信息异常")
    original = await first.request.all_headers()
    headers = {key: value for key, value in original.items()
               if key not in {"host", "content-length", "cookie", "accept-encoding"}}
    rows: list[dict] = []
    seen: set[str] = set()
    for number in range(1, max(1, count) + 1):
        response = first if number == 1 else await page.request.get(
            _page_url(first.url, field, number), headers=headers, timeout=timeout_ms)
        if not response.ok:
            raise SourceError(f"订单第 {number} 页返回 HTTP {response.status}")
        payload = await response.json()
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise SourceError(f"订单第 {number} 页返回错误")
        result = payload.get(data_key)
        batch = result.get(list_key) if isinstance(result, dict) else None
        if not isinstance(batch, list):
            raise SourceError("订单列表格式已改变")
        for row in batch:
            if not isinstance(row, dict) or not row.get("id"):
                raise SourceError("订单编号缺失")
            order_id = str(row["id"])
            if order_id in seen:
                raise SourceError("订单分页出现重复编号，已停止同步")
            seen.add(order_id)
        rows.extend(batch)
        progress(number, max(1, count))
    return rows


async def fetch_sonkwo_orders(playwright, settings: Settings,
                              progress: Callable[[str], None]) -> list[AccountOrder]:
    timeout_ms = int(settings.operation_timeout * 1000)
    progress("连接杉果订单")
    sonkwo_context = await playwright.chromium.launch_persistent_context(
        str(settings.profile_path("sonkwo")),
        **browser_launch_options(playwright, settings, True))
    try:
        page = sonkwo_context.pages[0] if sonkwo_context.pages else await sonkwo_context.new_page()
        if not await ensure_sonkwo_session(sonkwo_context, settings.profile_path("sonkwo"), timeout_ms, progress):
            raise SourceError("杉果续期凭证已失效；打开 /auth 点击杉果的“开始登录”")
        async with page.expect_response(
                lambda response: urlsplit(response.url).path == "/order/page",
                timeout=timeout_ms) as found:
            await page.goto(SONKWO_ORDERS, wait_until="domcontentloaded", timeout=timeout_ms)
        first = await found.value
        payload = await first.json()
        if not first.ok or not isinstance(payload, dict) or payload.get("success") is not True:
            raise SourceError(f"杉果订单接口未返回成功结果（HTTP {first.status}）；请检查会话是否过期")
        data = payload.get("data") if isinstance(payload, dict) else None
        meta = data.get("meta") if isinstance(data, dict) else None
        if not isinstance(meta, dict) or not isinstance(meta.get("total_pages"), int):
            keys = sorted(data)[:12] if isinstance(data, dict) else []
            raise SourceError(f"杉果订单分页格式已改变（data 字段：{keys}）")
        sonkwo_rows = await _all_pages(
            page, first, field="page", count=meta["total_pages"], data_key="data",
            list_key="list", expected_path="/order/page",
            progress=lambda number, count: progress(f"杉果订单 {number}/{count} 页"),
            timeout_ms=timeout_ms)
        if len(sonkwo_rows) != meta.get("total_count"):
            raise SourceError("杉果订单页数与总数不一致")
        sonkwo_orders = parse_sonkwo_orders(sonkwo_rows)
    finally:
        await sonkwo_context.close()
    return sonkwo_orders


async def fetch_steampy_orders(playwright, settings: Settings,
                               progress: Callable[[str], None]) -> list[AccountOrder]:
    timeout_ms = int(settings.operation_timeout * 1000)
    progress("连接 SteamPy 卖家订单")
    steampy_context = await playwright.chromium.launch_persistent_context(
        str(settings.profile_path("steampy")),
        **browser_launch_options(playwright, settings, True))
    try:
        page = steampy_context.pages[0] if steampy_context.pages else await steampy_context.new_page()
        if not await ensure_steampy_session(
                steampy_context, page, settings.profile_path("steampy"), timeout_ms):
            raise SourceError("SteamPy 卖家账号未登录；打开 /auth 点击 SteamPy 的“开始登录”")
        await page.get_by_text("卖家中心", exact=True).first.click(timeout=timeout_ms)
        await page.get_by_text("卖家中心-CDK", exact=True).first.click(timeout=timeout_ms)
        async with page.expect_response(
                lambda response: urlsplit(response.url).path == "/xboot/steamKeyOrder/cdkSearch",
                timeout=timeout_ms) as found_normal:
            await page.get_by_text("普通订单总列表", exact=True).first.click(timeout=timeout_ms)
        first_normal = await found_normal.value
        normal_payload = await first_normal.json()
        normal_result = normal_payload.get("result") if isinstance(normal_payload, dict) else None
        if not isinstance(normal_result, dict) or not isinstance(normal_result.get("pages"), int):
            raise SourceError("SteamPy 卖家订单分页格式已改变")
        normal_rows = await _all_pages(
            page, first_normal, field="pageNumber", count=normal_result["pages"],
            data_key="result", list_key="records",
            expected_path="/xboot/steamKeyOrder/cdkSearch",
            progress=lambda number, count: progress(f"SteamPy 订单 {number}/{count} 页"),
            timeout_ms=timeout_ms)
        if len(normal_rows) != normal_result.get("total"):
            raise SourceError("SteamPy 卖家订单页数与总数不一致")
        async with page.expect_response(
                lambda response: urlsplit(response.url).path == "/xboot/steamKeyOrder/cdkSearch"
                and parse_qs(urlsplit(response.url).query).get("type") == ["want"],
                timeout=timeout_ms) as found_request:
            await page.get_by_text("求购订单总列表", exact=True).first.click(timeout=timeout_ms)
        first_request = await found_request.value
        request_payload = await first_request.json()
        request_result = request_payload.get("result") if isinstance(request_payload, dict) else None
        if not isinstance(request_result, dict) or not isinstance(request_result.get("pages"), int):
            raise SourceError("SteamPy 求购订单分页格式已改变")
        request_rows = await _all_pages(
            page, first_request, field="pageNumber", count=request_result["pages"],
            data_key="result", list_key="records",
            expected_path="/xboot/steamKeyOrder/cdkSearch",
            progress=lambda number, count: progress(f"SteamPy 求购订单 {number}/{count} 页"),
            timeout_ms=timeout_ms)
        if len(request_rows) != request_result.get("total"):
            raise SourceError("SteamPy 求购订单页数与总数不一致")
        async with page.expect_response(
                lambda response: urlsplit(response.url).path == "/xboot/steamKeyOrder/listSelfSuccess",
                timeout=timeout_ms) as found_success:
            await page.get_by_text("成功订单列表", exact=True).first.click(timeout=timeout_ms)
        first_success = await found_success.value
        success_payload = await first_success.json()
        success_result = success_payload.get("result") if isinstance(success_payload, dict) else None
        if not isinstance(success_result, dict) or not isinstance(success_result.get("totalPages"), int):
            raise SourceError("SteamPy 成功订单分页格式已改变")
        success_rows = await _all_pages(
            page, first_success, field="pageNumber", count=success_result["totalPages"],
            data_key="result", list_key="content",
            expected_path="/xboot/steamKeyOrder/listSelfSuccess",
            progress=lambda number, count: progress(f"SteamPy 成功订单 {number}/{count} 页"),
            timeout_ms=timeout_ms)
        if len(success_rows) != success_result.get("totalElements"):
            raise SourceError("SteamPy 成功订单页数与总数不一致")
        steampy_orders = [*parse_steampy_orders(normal_rows, success_rows),
                          *parse_steampy_request_orders(request_rows)]
    finally:
        await steampy_context.close()
    return steampy_orders


async def fetch_account_orders(settings: Settings, progress: Callable[[str], None],
                               source: str = "both") -> list[AccountOrder]:
    if source not in {"both", "sonkwo", "steampy"}:
        raise ValueError("未知账号订单来源")
    async with playwright_session(settings.data_dir) as playwright:
        if source == "sonkwo":
            return await fetch_sonkwo_orders(playwright, settings, progress)
        if source == "steampy":
            return await fetch_steampy_orders(playwright, settings, progress)
        sonkwo_orders = await fetch_sonkwo_orders(playwright, settings, progress)
        steampy_orders = await fetch_steampy_orders(playwright, settings, progress)
    return [*sonkwo_orders, *steampy_orders]


class OrderSyncService:
    def __init__(self, settings: Settings, repository: Repository, scanner=None):
        self.settings = settings
        self.repository = repository
        self.scanner = scanner
        self._task: asyncio.Task | None = None
        self._started_monotonic: float | None = None
        self._state = {"status": "idle", "stage": "idle", "error": None,
                       "started_at": None, "last_activity": None,
                       "orders": None, "warnings": []}

    def status(self) -> dict:
        state = dict(self._state)
        state["elapsed_seconds"] = (round(time.monotonic() - self._started_monotonic, 1)
                                     if state["status"] == "running" and self._started_monotonic else None)
        return state

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            raise SourceError("历史订单同步已在运行")
        if self.scanner is not None and self.scanner.status()["status"] == "running":
            raise SourceError("扫描正在运行，请等扫描完成再同步历史订单")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._started_monotonic = time.monotonic()
        self._state = {"status": "running", "stage": "连接账号", "error": None,
                       "started_at": now, "last_activity": now,
                       "orders": None, "warnings": []}
        self._task = asyncio.create_task(self._run())

    async def wait(self) -> dict:
        if self._task is not None:
            await asyncio.shield(self._task)
        return self.status()

    async def cancel(self) -> bool:
        if self._task is None or self._task.done():
            return False
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        return True

    async def _run(self) -> None:
        def progress(stage: str) -> None:
            self._state["stage"] = stage
            self._state["last_activity"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

        try:
            async with asyncio.timeout(self.settings.run_timeout):
                previous = self.repository.account_order_summary()
                source_times = dict(previous["source_synced_at"])
                coverage = set(previous["source_coverage"])
                sources: dict[str, list[AccountOrder]] = {}
                warnings: list[str] = []
                refreshed = 0
                for source, label in (("sonkwo", "杉果"), ("steampy", "SteamPy")):
                    try:
                        sources[source] = await fetch_account_orders(
                            self.settings, progress, source=source)
                    except Exception as exc:
                        cached = self.repository.saved_account_orders(source)
                        if not cached:
                            raise SourceError(f"{label}读取失败且无已保存快照：{exc}") from exc
                        sources[source] = cached
                        warnings.append(f"{label}读取失败，沿用上次完整快照：{str(exc)[:140]}")
                        progress(f"{label}沿用上次快照")
                    else:
                        refreshed += 1
                        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                        if source == "sonkwo":
                            source_times["sonkwo"] = now
                        else:
                            source_times.update({"ordinary": now, "request": now})
                            coverage.update(("ordinary", "request"))
                if refreshed == 0:
                    raise SourceError("杉果与 SteamPy 均未更新；" + "；".join(warnings))
                orders = [*sources["sonkwo"], *sources["steampy"]]
                progress("保存订单统计")
                self.repository.replace_account_orders(
                    orders, TitleCatalog.from_file(self.settings.aliases_path),
                    source_coverage=tuple(sorted(coverage)),
                    source_synced_at=source_times,
                    warnings=tuple(warnings))
                self._state["status"] = "completed_with_warnings" if warnings else "completed"
                self._state["orders"] = len(orders)
                self._state["warnings"] = warnings
        except asyncio.CancelledError:
            self._state["status"] = "cancelled"
            self._state["error"] = "订单同步已取消；保留上次完整结果"
        except Exception as exc:
            self._state["status"] = "failed"
            self._state["error"] = str(exc)[:200]
        finally:
            progress("finished")
