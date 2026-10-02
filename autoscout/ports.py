"""Boundaries between the workflow and external platforms."""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncContextManager, Callable, Protocol

from .domain import MarketLookup, Offer


class SourceError(RuntimeError):
    """An external platform could not provide a trustworthy snapshot."""


@dataclass(frozen=True, slots=True)
class FetchBatch:
    offers: tuple[Offer, ...]
    warnings: tuple[str, ...] = ()


class OfferSource(Protocol):
    async def list_offers(self, keyword: str, page: int, status: str) -> FetchBatch: ...


class QuoteSource(Protocol):
    async def lookup(self, offer: Offer) -> MarketLookup: ...


@dataclass(slots=True)
class Sources:
    offers: OfferSource
    quotes: QuoteSource


SourceFactory = Callable[[], AsyncContextManager[Sources]]
