"""Bounded, observable scan workflow."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import logging
import time
from uuid import uuid4

from .domain import Assessment, Verdict, assess
from .demand import market_trend, scoped_snapshot
from .products import ProductMatch
from .ports import SourceFactory
from .repository import Repository
from .settings import Settings
from .titles import TitleCatalog


log = logging.getLogger(__name__)


class ScanBusy(RuntimeError):
    pass


class ScanService:
    def __init__(self, settings: Settings, repository: Repository, catalog: TitleCatalog,
                 source_factory: SourceFactory):
        self.settings = settings
        self.repository = repository
        repository.reprice_legacy_assessments(settings.policy)
        repository.reclassify_legacy_demand()
        self.catalog = catalog
        self.source_factory = source_factory
        self._task: asyncio.Task | None = None
        self._state: dict = {"status": "idle", "stage": "idle", "run_id": None,
                             "discovered": 0, "processed": 0, "opportunities": 0,
                             "warning_count": 0, "error_count": 0,
                             "last_warning": None, "error": None,
                             "started_at": None, "last_activity": None}
        self._started_monotonic: float | None = None

    def status(self) -> dict:
        result = dict(self._state)
        result["elapsed_seconds"] = (
            round(time.monotonic() - self._started_monotonic, 1)
            if result["status"] == "running" and self._started_monotonic is not None else None
        )
        return result

    def _progress(self, stage: str) -> None:
        self._state["stage"] = stage
        self._state["last_activity"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        run_id = self._state["run_id"]
        if run_id:
            self.repository.update_run(run_id, self._state["status"], stage,
                                       self._state["discovered"], self._state["processed"],
                                       self._state["opportunities"], self._state["warning_count"])

    def start(self, keyword: str = "", pages: int | None = None) -> str:
        if self._task is not None and not self._task.done():
            raise ScanBusy("已有扫描在运行")
        page_count = pages if pages is not None else self.settings.max_pages
        if not 1 <= page_count <= self.settings.max_pages:
            raise ValueError(f"页数必须在 1 到 {self.settings.max_pages} 之间")
        keyword = keyword.strip()
        if len(keyword) > 80:
            raise ValueError("关键词过长")
        run_id = uuid4().hex
        self.repository.begin_run(run_id, keyword)
        self._started_monotonic = time.monotonic()
        self._state = {"status": "running", "stage": "connecting", "run_id": run_id,
                       "discovered": 0, "processed": 0, "opportunities": 0,
                       "warning_count": 0, "error_count": 0,
                       "last_warning": None, "error": None,
                       "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       "last_activity": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        self._task = asyncio.create_task(self._run(keyword, page_count))
        return run_id

    async def wait(self) -> dict:
        if self._task is not None:
            await asyncio.shield(self._task)
        return self.status()

    async def cancel(self) -> bool:
        if self._task is None or self._task.done():
            return False
        self._task.cancel()
        await self._task
        return True

    async def _run(self, keyword: str, page_count: int) -> None:
        run_id = self._state["run_id"]
        final_status, error = "completed", None
        try:
            async with asyncio.timeout(self.settings.run_timeout):
                async with self.source_factory() as sources:
                    seen_urls: set[str] = set()
                    for mode in ("lowest", "new_lowest"):
                        for page in range(1, page_count + 1):
                            self._progress(f"采集杉果 {mode} 第 {page} 页")
                            batch = await asyncio.wait_for(
                                sources.offers.list_offers(keyword, page, mode),
                                self.settings.operation_timeout,
                            )
                            for warning in batch.warnings:
                                self._state["warning_count"] += 1
                                self._state["last_warning"] = warning
                                log.warning(warning)
                            if not batch.offers and not keyword:
                                break
                            fresh = [offer for offer in batch.offers if offer.url not in seen_urls]
                            self._state["discovered"] += len(fresh)
                            for offer in fresh:
                                seen_urls.add(offer.url)
                                if self._state["processed"] >= self.settings.max_offers:
                                    break
                                self._progress(f"核价：{offer.title[:60]}")
                                try:
                                    verifier = getattr(sources.offers, "verify_offer", None)
                                    if verifier:
                                        self._progress(f"核对杉果商品详情：{offer.title[:60]}")
                                        offer = await asyncio.wait_for(verifier(offer), self.settings.operation_timeout)
                                    self._progress(f"核对 SteamPy 商品、挂价与求购：{offer.title[:60]}")
                                    lookup = await asyncio.wait_for(
                                        sources.quotes.lookup(offer), self.settings.operation_timeout
                                    )
                                    if lookup.quote and scoped_snapshot(lookup.quote.demand, lookup.quote.product):
                                        demand = dict(lookup.quote.demand)
                                        demand["trend"] = market_trend(demand, self.repository.market_history(demand))
                                        lookup = replace(lookup, quote=replace(lookup.quote, demand=demand))
                                    mapping = self.repository.product_mapping(offer.product.product_id) if offer.product else None
                                    result = assess(offer, lookup, self.catalog, self.settings.policy, mapping)
                                except Exception as exc:
                                    log.exception("核价失败：%s", offer.title)
                                    message = str(exc)[:200] or "商品核对超时，请稍后重新扫描"
                                    result = Assessment(offer, Verdict.ERROR, message,
                                                        matching=ProductMatch(False, message, blocked=True).evidence(offer))
                                    self._state["warning_count"] += 1
                                    self._state["error_count"] += 1
                                    self._state["last_warning"] = result.reason
                                self.repository.save_assessment(run_id, result)
                                self._state["processed"] += 1
                                if result.verdict == Verdict.OPPORTUNITY:
                                    self._state["opportunities"] += 1
                                self._progress("保存扫描结果")
                            if self._state["processed"] >= self.settings.max_offers:
                                break
                        if self._state["processed"] >= self.settings.max_offers:
                            break
            if (self._state["processed"] > 0
                    and self._state["error_count"] == self._state["processed"]):
                final_status, error = "failed", "全部商品核价失败；请检查 SteamPy 登录状态或页面结构"
            elif self._state["warning_count"]:
                final_status = "completed_with_warnings"
        except asyncio.CancelledError:
            final_status, error = "cancelled", "用户取消扫描"
        except TimeoutError:
            final_status, error = "failed", "扫描超时；请缩小范围或检查平台连接"
            log.exception(error)
        except Exception as exc:
            final_status, error = "failed", str(exc)[:200]
            log.exception("扫描失败")
        finally:
            self._state["status"] = final_status
            self._state["stage"] = "finished"
            self._state["error"] = error
            self._state["last_activity"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self.repository.update_run(run_id, final_status, "finished", self._state["discovered"],
                                       self._state["processed"], self._state["opportunities"],
                                       self._state["warning_count"], error, finished=True)
