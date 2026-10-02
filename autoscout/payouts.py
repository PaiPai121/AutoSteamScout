"""Read-only discovery of the SteamPy payout evidence available to the account."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import time
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from .browser import browser_launch_options
from .domain import money
from .playwright_runtime import playwright_session
from .ports import SourceError
from .repository import Repository
from .session_restore import ensure_steampy_session
from .settings import Settings


WALLET_HISTORY_START = "2010-01-01T00:00:00.000Z"
MAX_WALLET_PAGES = 500


@dataclass(frozen=True, slots=True)
class WalletBill:
    bill_id: str
    occurred_at: str
    amount: Decimal
    tx_type: str
    tx_id: str
    cd_flag: str


@dataclass(frozen=True, slots=True)
class WalletSnapshot:
    bills: list[WalletBill]
    balance: Decimal
    pending_balance: Decimal
    balance_at: str


def parse_wallet_balance(payload: dict) -> tuple[Decimal, Decimal]:
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or payload.get("success") is not True or not isinstance(result, dict):
        raise SourceError("SteamPy 钱包余额未返回成功结果，保留上次完整快照")
    values = []
    for key in ("balance", "pendingBalance"):
        try:
            value = Decimal(str(result[key]))
            if not value.is_finite():
                raise ValueError("non-finite balance")
            values.append(money(value))
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            raise SourceError("SteamPy 钱包余额金额无效，保留上次完整快照") from exc
    return values[0], values[1]


def parse_wallet_bills(rows: list[dict]) -> list[WalletBill]:
    """Keep only accounting metadata; discard users, payment accounts and descriptions."""
    bills = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or not row.get("id"):
            raise SourceError("SteamPy 钱包流水缺少编号")
        bill_id = str(row["id"])
        if bill_id in seen:
            raise SourceError("SteamPy 钱包分页出现重复流水")
        seen.add(bill_id)
        occurred_at = row.get("createTime")
        tx_type = row.get("txType")
        if not isinstance(occurred_at, str) or not occurred_at:
            raise SourceError("SteamPy 钱包流水时间无效")
        try:
            datetime.fromisoformat(occurred_at)
        except ValueError as exc:
            raise SourceError("SteamPy 钱包流水时间无效") from exc
        if not isinstance(tx_type, str) or not tx_type:
            raise SourceError("SteamPy 钱包流水类型无效")
        try:
            amount = Decimal(str(row["amount"]))
            if not amount.is_finite():
                raise ValueError("non-finite amount")
            amount = money(amount)
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            raise SourceError("SteamPy 钱包流水金额无效") from exc
        bills.append(WalletBill(bill_id, occurred_at, amount, tx_type,
                                str(row.get("txId") or ""), str(row.get("cdFlag") or "")))
    return bills


async def fetch_wallet_snapshot(settings: Settings, progress: Callable[[str], None]) -> WalletSnapshot:
    """Read complete history and the official balance through the same account session."""
    timeout_ms = int(settings.operation_timeout * 1000)
    async with playwright_session(settings.data_dir) as playwright:
        context = await playwright.chromium.launch_persistent_context(
            str(settings.profile_path("steampy")),
            **browser_launch_options(playwright, settings, True))
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            progress("连接 SteamPy 钱包流水")
            if not await ensure_steampy_session(
                    context, page, settings.profile_path("steampy"), timeout_ms):
                raise SourceError("SteamPy 钱包账号未登录；打开 /auth 点击 SteamPy 的“开始登录”")
            async with page.expect_response(
                    lambda response: urlsplit(response.url).path == "/xboot/walletBill/getList",
                    timeout=timeout_ms) as found:
                await page.goto("https://steampy.com/pcPyrecord", wait_until="domcontentloaded",
                                timeout=timeout_ms)
            first = await found.value
            parts = urlsplit(first.url)
            query = parse_qs(parts.query)
            if not {"startDate", "endDate", "pageNumber", "pageSize"}.issubset(query):
                raise SourceError("SteamPy 钱包查询参数已改变")
            query["startDate"] = [WALLET_HISTORY_START]
            query["pageNumber"] = ["1"]
            headers_original = await first.request.all_headers()
            headers = {key: value for key, value in headers_original.items()
                       if key not in {"host", "content-length", "cookie", "accept-encoding"}}

            def url_for(number: int) -> str:
                query["pageNumber"] = [str(number)]
                return urlunsplit((parts.scheme, parts.netloc, parts.path,
                                   urlencode(query, doseq=True), ""))

            all_rows: list[dict] = []
            total_pages = None
            total_elements = None
            number = 1
            while total_pages is None or number <= total_pages:
                response = await page.request.get(url_for(number), headers=headers, timeout=timeout_ms)
                if not response.ok:
                    raise SourceError(f"SteamPy 钱包第 {number} 页返回 HTTP {response.status}")
                payload = await response.json()
                result = payload.get("result") if isinstance(payload, dict) else None
                if (not isinstance(payload, dict) or payload.get("success") is not True
                        or not isinstance(result, dict)
                        or not isinstance(result.get("content"), list)
                        or not isinstance(result.get("totalPages"), int)
                        or not isinstance(result.get("totalElements"), int)):
                    raise SourceError("SteamPy 钱包分页格式已改变，保留上次完整结果")
                if total_pages is None:
                    total_pages = result["totalPages"]
                    total_elements = result["totalElements"]
                    if not 0 <= total_pages <= MAX_WALLET_PAGES or total_elements < 0:
                        raise SourceError("SteamPy 钱包历史页数异常")
                elif (result["totalPages"] != total_pages
                      or result["totalElements"] != total_elements):
                    raise SourceError("SteamPy 钱包分页期间记录数量变化，请重试")
                all_rows.extend(result["content"])
                progress(f"SteamPy 钱包流水 {number}/{max(total_pages, 1)} 页")
                number += 1
            if len(all_rows) != total_elements:
                raise SourceError("SteamPy 钱包页数与流水总数不一致")
            bills = parse_wallet_bills(all_rows)
            progress("核对 SteamPy 官方钱包余额")
            try:
                response = await context.request.get(
                    "https://steampy.com/xboot/payWallet/getBalance", headers=headers, timeout=timeout_ms)
                if not response.ok:
                    raise SourceError(f"SteamPy 钱包余额返回 HTTP {response.status}，保留上次完整快照")
                balance, pending_balance = parse_wallet_balance(await response.json())
            except SourceError:
                raise
            except Exception:
                # A Playwright request trace may contain authentication headers.
                raise SourceError("SteamPy 钱包余额连接失败，保留上次完整快照；可稍后重试") from None
            return WalletSnapshot(bills, balance, pending_balance,
                                  datetime.now(timezone.utc).isoformat(timespec="seconds"))
        finally:
            await context.close()


async def fetch_wallet_bills(settings: Settings, progress: Callable[[str], None]) -> list[WalletBill]:
    """Compatibility for consumers that only need bill metadata."""
    return (await fetch_wallet_snapshot(settings, progress)).bills


class PayoutSyncService:
    def __init__(self, settings: Settings, repository: Repository, scanner=None, order_sync=None):
        self.settings = settings
        self.repository = repository
        self.scanner = scanner
        self.order_sync = order_sync
        self._task: asyncio.Task | None = None
        self._started_monotonic: float | None = None
        self._state = {"status": "idle", "stage": "idle", "error": None,
                       "started_at": None, "last_activity": None, "bills": None}

    def status(self) -> dict:
        state = dict(self._state)
        state["elapsed_seconds"] = (round(time.monotonic() - self._started_monotonic, 1)
                                    if state["status"] == "running" and self._started_monotonic else None)
        return state

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            raise SourceError("钱包流水同步已在运行")
        if self.scanner is not None and self.scanner.status()["status"] == "running":
            raise SourceError("扫描正在运行，请等扫描完成再同步钱包")
        if self.order_sync is not None and self.order_sync.status()["status"] == "running":
            raise SourceError("订单同步正在运行，请等订单同步完成再同步钱包")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._started_monotonic = time.monotonic()
        self._state = {"status": "running", "stage": "连接账号", "error": None,
                       "started_at": now, "last_activity": now, "bills": None}
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
                snapshot = await fetch_wallet_snapshot(self.settings, progress)
                progress("保存钱包流水与余额")
                self.repository.replace_wallet_bills(
                    snapshot.bills, balance=snapshot.balance, pending_balance=snapshot.pending_balance,
                    balance_at=snapshot.balance_at)
                self._state["status"] = "completed"
                self._state["bills"] = len(snapshot.bills)
        except asyncio.CancelledError:
            self._state["status"] = "cancelled"
            self._state["error"] = "钱包同步已取消；保留上次完整结果"
        except Exception as exc:
            self._state["status"] = "failed"
            self._state["error"] = str(exc)[:200]
        finally:
            progress("finished")


async def inspect_payout_navigation(settings: Settings) -> dict:
    """Inspect finance navigation without triggering a transfer or saving response bodies."""
    async with playwright_session(settings.data_dir) as playwright:
        context = await playwright.chromium.launch_persistent_context(
            str(settings.profile_path("steampy")),
            **browser_launch_options(playwright, settings, True))
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            paths: list[str] = []
            wallet_shapes: list[dict] = []
            first_wallet = None

            async def inspect_response(response) -> None:
                nonlocal first_wallet
                path = urlsplit(response.url).path
                if response.request.resource_type not in {"xhr", "fetch"}:
                    return
                paths.append(path)
                if path != "/xboot/walletBill/getList":
                    return
                if first_wallet is None:
                    first_wallet = response
                try:
                    payload = await response.json()
                except Exception:
                    return
                def shape(value, depth=0):
                    if depth >= 5:
                        return type(value).__name__
                    if isinstance(value, dict):
                        return {key: shape(item, depth + 1) for key, item in value.items()}
                    if isinstance(value, list):
                        return {"count": len(value), "first": shape(value[0], depth + 1) if value else None}
                    return type(value).__name__
                result = payload.get("result") if isinstance(payload, dict) else None
                bills = result.get("content", []) if isinstance(result, dict) else []
                wallet_shapes.append({"http_status": response.status, "shape": shape(payload),
                                      "request_method": response.request.method,
                                      "request_query_keys": sorted(parse_qs(urlsplit(response.url).query)),
                                      "request_query": {key: parse_qs(urlsplit(response.url).query).get(key)
                                                        for key in ("startDate", "endDate", "pageNumber", "pageSize")},
                                      "request_body_keys": sorted(json.loads(response.request.post_data).keys())
                                      if response.request.post_data and response.request.post_data.startswith("{") else [],
                                      "total_elements": result.get("totalElements") if isinstance(result, dict) else None,
                                      "bills": [{key: bill.get(key) for key in
                                                 ("createTime", "amount", "cdFlag", "txType")}
                                                for bill in bills if isinstance(bill, dict)]})

            page.on("response", inspect_response)
            await page.goto("https://steampy.com/home", wait_until="domcontentloaded",
                            timeout=int(settings.operation_timeout * 1000))
            await asyncio.sleep(1)
            stages = []

            async def snapshot(name: str) -> None:
                text = await page.locator("body").inner_text()
                labels = [word for word in ("提现记录", "提现", "钱包", "财务", "资金", "余额", "账单", "明细")
                          if word in text]
                targets = []
                for label in ("提现记录", "提现", "交易明细", "账单明细", "明细", "钱包"):
                    locator = page.get_by_text(label, exact=True)
                    for index in range(min(await locator.count(), 3)):
                        element = locator.nth(index)
                        if await element.is_visible():
                            targets.append({"label": label,
                                            "tag": await element.evaluate("node => node.tagName"),
                                            "href_path": urlsplit(await element.get_attribute("href") or "").path})
                stages.append({"stage": name, "route": urlsplit(page.url).path,
                               "labels": labels, "targets": targets,
                               "api_paths": list(dict.fromkeys(paths))[-30:]})

            await snapshot("home")
            seller = page.get_by_text("卖家中心", exact=True)
            if await seller.count() and await seller.first.is_visible():
                await seller.first.click(timeout=10000)
                await asyncio.sleep(1)
                await snapshot("seller_menu")
            for label in ("提现记录", "账单明细", "交易明细", "明细", "财务中心", "资金管理", "我的钱包", "钱包"):
                target = page.get_by_text(label, exact=True)
                if await target.count() and await target.first.is_visible():
                    await target.first.click(timeout=10000)
                    await asyncio.sleep(1)
                    await snapshot("finance_page")
                    break
            history_probe = None
            if first_wallet is not None:
                parts = urlsplit(first_wallet.url)
                query = parse_qs(parts.query)
                query["startDate"] = ["2020-01-01T00:00:00.000Z"]
                history_url = urlunsplit((parts.scheme, parts.netloc, parts.path,
                                          urlencode(query, doseq=True), ""))
                original = await first_wallet.request.all_headers()
                headers = {key: value for key, value in original.items()
                           if key not in {"host", "content-length", "cookie", "accept-encoding"}}
                response = await page.request.get(history_url, headers=headers, timeout=int(settings.operation_timeout * 1000))
                payload = await response.json()
                result = payload.get("result") if isinstance(payload, dict) else None
                content = result.get("content", []) if isinstance(result, dict) else []
                history_probe = {"http_status": response.status,
                                 "success": payload.get("success") if isinstance(payload, dict) else None,
                                 "code": payload.get("code") if isinstance(payload, dict) else None,
                                 "total_elements": result.get("totalElements") if isinstance(result, dict) else None,
                                 "total_pages": result.get("totalPages") if isinstance(result, dict) else None,
                                 "first_page_types": sorted({bill.get("txType") for bill in content
                                                             if isinstance(bill, dict) and bill.get("txType")})}
            return {"stages": stages, "wallet_shapes": wallet_shapes,
                    "history_probe": history_probe}
        finally:
            await context.close()
