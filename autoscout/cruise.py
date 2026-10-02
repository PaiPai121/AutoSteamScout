"""Automatic, pausable scan cycles over configured search keywords."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging

from .scanner import ScanService
from .settings import Settings
from .orders import OrderSyncService
from .payouts import PayoutSyncService


log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class CruiseService:
    def __init__(self, settings: Settings, scanner: ScanService,
                 order_sync: OrderSyncService | None = None,
                 payout_sync: PayoutSyncService | None = None):
        self.settings = settings
        self.scanner = scanner
        self.order_sync = order_sync
        self.payout_sync = payout_sync
        self._task: asyncio.Task | None = None
        self._owned_run_id: str | None = None
        self._owns_order_sync = False
        self._owns_payout_sync = False
        self._paused = True
        self._state = {
            "cycle": 0, "current_keyword": None, "last_run_id": None,
            "last_result": None, "last_error": None, "last_activity": None,
            "next_run_at": None, "last_order_error": None, "last_payout_error": None,
        }

    def status(self) -> dict:
        return {
            "enabled": not self._paused,
            "running": self._task is not None and not self._task.done(),
            "interval_seconds": self.settings.scan_interval,
            "keywords": self.settings.scan_keywords,
            **self._state,
        }

    def resume(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._paused = False
        initial_delay = self._initial_delay()
        self._state["next_run_at"] = (_now() + timedelta(seconds=initial_delay)).isoformat(timespec="seconds")
        self._task = asyncio.create_task(self._loop(initial_delay))

    def _initial_delay(self) -> float:
        """Keep a recently completed single-keyword scan when the server restarts."""
        if len(self.settings.scan_keywords) != 1:
            return 0.0
        runs = self.scanner.repository.runs(1)
        if not runs:
            return 0.0
        latest = runs[0]
        if (latest["status"] not in {"completed", "completed_with_warnings"}
                or latest["keyword"] != self.settings.scan_keywords[0].strip()
                or not latest["finished_at"]):
            return 0.0
        try:
            age = (_now() - datetime.fromisoformat(latest["finished_at"])).total_seconds()
        except ValueError:
            return 0.0
        return max(0.0, self.settings.scan_interval - max(0.0, age))

    async def pause(self) -> None:
        self._paused = True
        self._state["next_run_at"] = None
        owned_run_id = self._owned_run_id
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if owned_run_id is not None and self.scanner.status()["run_id"] == owned_run_id:
            await self.scanner.cancel()
        if self._owns_order_sync and self.order_sync is not None:
            await self.order_sync.cancel()
            self._owns_order_sync = False
        if self._owns_payout_sync and self.payout_sync is not None:
            await self.payout_sync.cancel()
            self._owns_payout_sync = False
        self._task = None

    async def _sync_orders(self) -> None:
        if self.order_sync is None:
            return
        snapshot = self.order_sync.repository.account_order_summary()["last_synced_at"]
        if snapshot is not None:
            try:
                if (_now() - datetime.fromisoformat(snapshot)).total_seconds() < self.settings.scan_interval:
                    return
            except ValueError:
                pass
        try:
            if self.scanner.status()["status"] == "running":
                await self.scanner.wait()
            if self.order_sync.status()["status"] != "running":
                self.order_sync.start()
                self._owns_order_sync = True
            result = await self.order_sync.wait()
            self._owns_order_sync = False
            warning_text = "；".join(result.get("warnings") or [])
            self._state["last_order_error"] = result["error"] or warning_text or None
            if result["status"] not in {"completed", "completed_with_warnings"}:
                log.warning("账号订单同步未完成：%s", result["error"])
            elif warning_text:
                log.warning("账号订单部分来源沿用旧快照：%s", warning_text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._state["last_order_error"] = str(exc)[:200]
            log.exception("账号订单同步无法启动")

    async def _sync_payouts(self) -> None:
        if self.payout_sync is None:
            return
        snapshot = self.payout_sync.repository.wallet_payouts()["last_synced_at"]
        if snapshot is not None:
            try:
                if (_now() - datetime.fromisoformat(snapshot)).total_seconds() < self.settings.scan_interval:
                    return
            except ValueError:
                pass
        try:
            if self.order_sync is not None and self.order_sync.status()["status"] == "running":
                await self.order_sync.wait()
            if self.scanner.status()["status"] == "running":
                await self.scanner.wait()
            if self.payout_sync.status()["status"] != "running":
                self.payout_sync.start()
                self._owns_payout_sync = True
            result = await self.payout_sync.wait()
            self._owns_payout_sync = False
            self._state["last_payout_error"] = result["error"]
            if result["status"] != "completed":
                log.warning("钱包流水同步未完成：%s", result["error"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._state["last_payout_error"] = str(exc)[:200]
            log.exception("钱包流水同步无法启动")

    async def _loop(self, initial_delay: float = 0.0) -> None:
        try:
            if initial_delay > 0:
                await self._sync_orders()
                await self._sync_payouts()
                await asyncio.sleep(initial_delay)
            while not self._paused:
                self._state["cycle"] += 1
                await self._sync_orders()
                await self._sync_payouts()
                for raw_keyword in self.settings.scan_keywords:
                    if self._paused:
                        break
                    keyword = raw_keyword.strip()
                    self._state["current_keyword"] = keyword
                    self._state["last_activity"] = _now().isoformat(timespec="seconds")
                    self._state["next_run_at"] = None
                    if self.scanner.status()["status"] == "running":
                        await self.scanner.wait()
                    if self.order_sync is not None and self.order_sync.status()["status"] == "running":
                        await self.order_sync.wait()
                    try:
                        run_id = self.scanner.start(keyword, self.settings.max_pages)
                        self._owned_run_id = run_id
                        self._state["last_run_id"] = run_id
                        result = await self.scanner.wait()
                        self._state["last_result"] = result["status"]
                        self._state["last_error"] = result["error"]
                    except Exception as exc:
                        self._state["last_result"] = "failed"
                        self._state["last_error"] = str(exc)[:200]
                        log.exception("自动巡航无法启动")
                    finally:
                        self._owned_run_id = None
                    self._state["last_activity"] = _now().isoformat(timespec="seconds")
                if self._paused:
                    break
                self._state["current_keyword"] = None
                self._state["next_run_at"] = (_now() + timedelta(seconds=self.settings.scan_interval)).isoformat(timespec="seconds")
                await asyncio.sleep(self.settings.scan_interval)
        except asyncio.CancelledError:
            raise
        finally:
            self._state["current_keyword"] = None
            if self._paused:
                self._state["next_run_at"] = None
