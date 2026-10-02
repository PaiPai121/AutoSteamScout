from contextlib import asynccontextmanager
from decimal import Decimal

import pytest

from autoscout.domain import MarketLookup, MarketQuote, Offer
from autoscout.ports import FetchBatch, Sources
from autoscout.repository import Repository
from autoscout.scanner import ScanService
from autoscout.settings import Settings
from autoscout.titles import TitleCatalog


class FakeOffers:
    async def list_offers(self, keyword, page, status):
        if page > 1:
            return FetchBatch(())
        return FetchBatch((Offer("Game", "https://www.sonkwo.cn/store/1", Decimal("80")),))


class FakeQuotes:
    async def lookup(self, offer):
        return MarketLookup(MarketQuote("Game", (Decimal("100"), Decimal("90"))))


@asynccontextmanager
async def fake_sources():
    yield Sources(FakeOffers(), FakeQuotes())


@pytest.mark.asyncio
async def test_scan_deduplicates_modes_and_persists_progress(tmp_path):
    settings = Settings(tmp_path, max_pages=2)
    repo = Repository(settings.database_path)
    service = ScanService(settings, repo, TitleCatalog(), fake_sources)
    run_id = service.start("", 2)
    state = await service.wait()
    assert state["status"] == "completed"
    assert state["processed"] == 1
    assert state["opportunities"] == 0  # an asking spread alone cannot qualify
    assert len(repo.assessments(run_id)) == 1
    assert repo.runs()[0]["status"] == "completed"
