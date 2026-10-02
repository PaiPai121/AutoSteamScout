import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal

import pytest

from autoscout.cruise import CruiseService
from autoscout.domain import MarketLookup, MarketQuote, Offer
from autoscout.ports import FetchBatch, Sources
from autoscout.repository import Repository
from autoscout.scanner import ScanService
from autoscout.settings import Settings
from autoscout.titles import TitleCatalog


class Offers:
    async def list_offers(self, keyword, page, status):
        return FetchBatch((Offer("Game", "https://www.sonkwo.cn/store/1", Decimal("50")),))


class Quotes:
    async def lookup(self, offer):
        return MarketLookup(MarketQuote("Game", (Decimal("80"),)))


@asynccontextmanager
async def sources():
    yield Sources(Offers(), Quotes())


@pytest.mark.asyncio
async def test_automatic_cruise_repeats_and_stats_do_not_double_count_profit(tmp_path):
    settings = Settings(tmp_path, auto_scan=True, scan_interval=0.03, max_pages=1)
    repo = Repository(settings.database_path)
    scanner = ScanService(settings, repo, TitleCatalog(), sources)
    cruise = CruiseService(settings, scanner)
    cruise.resume()
    try:
        async def wait_for_two_runs():
            while len(repo.runs()) < 2 or repo.runs()[0]["status"] == "running":
                await asyncio.sleep(0.01)
        await asyncio.wait_for(wait_for_two_runs(), timeout=3)
    finally:
        await cruise.pause()
    assert cruise.status()["enabled"] is False
    stats = repo.scan_statistics()
    assert stats["unique_offers"] == 1
    assert stats["latest_verdicts"]["price_only"] == 1
    assert stats["latest_estimated_profit"] == "0.00"
    assert stats["latest_asking_profit"] == "26.82"
    assert len(repo.runs()) >= 2


@pytest.mark.asyncio
async def test_pausing_cruise_does_not_cancel_a_manual_scan(tmp_path):
    started = asyncio.Event()
    release = asyncio.Event()

    class SlowOffers:
        async def list_offers(self, keyword, page, status):
            started.set()
            await release.wait()
            return FetchBatch(())

    @asynccontextmanager
    async def slow_sources():
        yield Sources(SlowOffers(), Quotes())

    settings = Settings(tmp_path, max_pages=1)
    repo = Repository(settings.database_path)
    scanner = ScanService(settings, repo, TitleCatalog(), slow_sources)
    cruise = CruiseService(settings, scanner)
    manual_run_id = scanner.start("manual", 1)
    await started.wait()
    cruise.resume()
    await asyncio.sleep(0)
    await cruise.pause()
    assert scanner.status()["status"] == "running"
    assert scanner.status()["run_id"] == manual_run_id
    release.set()
    assert (await scanner.wait())["status"] == "completed"


@pytest.mark.asyncio
async def test_cruise_restart_preserves_recent_completed_scan(tmp_path):
    settings = Settings(tmp_path, auto_scan=True, scan_interval=60, max_pages=1)
    repo = Repository(settings.database_path)
    repo.begin_run("recent", "")
    repo.update_run("recent", "completed", "finished", 1, 1, 0, 0, finished=True)
    scanner = ScanService(settings, repo, TitleCatalog(), sources)
    cruise = CruiseService(settings, scanner)
    cruise.resume()
    try:
        await asyncio.sleep(0.05)
        assert len(repo.runs()) == 1
        assert cruise.status()["next_run_at"] is not None
    finally:
        await cruise.pause()
