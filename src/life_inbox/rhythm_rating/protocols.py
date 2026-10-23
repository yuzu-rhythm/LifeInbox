"""Integration seams for collectors, persistence, rendering, and publishing."""

from __future__ import annotations

from collections.abc import Awaitable
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from .change_detection import ChangeDetection
from .enums import Game
from .models import (
    CollectionRequest,
    CollectionResult,
    RatingSnapshot,
    RenderedImageResult,
)
from .planning import DiscordPublicationPlan, DiscordPublicationResult
from .spending import DailySpendingSummary


@runtime_checkable
class Collector(Protocol):
    """Per-game collector contract with no credential or browser assumption."""

    game: Game

    def collect(self, request: CollectionRequest) -> Awaitable[CollectionResult]:
        """Collect one source through an already-authorized integration."""


@runtime_checkable
class RatingSnapshotRepository(Protocol):
    """Persistence seam for latest and historical normalized snapshots."""

    def get_latest(self, *, source: str, game: Game) -> RatingSnapshot | None:
        """Return the latest comparable snapshot, if one exists."""

    def save(self, snapshot: RatingSnapshot) -> None:
        """Persist a normalized snapshot without exposing provider credentials."""


@runtime_checkable
class RatingImageRenderer(Protocol):
    """Renderer contract returning a detached image result."""

    def render(self, snapshot: RatingSnapshot) -> RenderedImageResult:
        """Render a snapshot or return a classified failure result."""


@runtime_checkable
class RatingChangeDetector(Protocol):
    """Pure comparison contract for semantic and image identity."""

    def compare(
        self,
        current: RatingSnapshot,
        previous: RatingSnapshot | None = None,
        *,
        current_image_sha256: str | None = None,
        previous_image_sha256: str | None = None,
    ) -> ChangeDetection:
        """Compare snapshots without reading a database or calling a provider."""


@runtime_checkable
class DiscordRatingPublisher(Protocol):
    """Publication seam for a previously planned rating message."""

    def publish(
        self, plan: DiscordPublicationPlan
    ) -> Awaitable[DiscordPublicationResult] | DiscordPublicationResult:
        """Publish one safe plan, or return a classified failure."""


@runtime_checkable
class DailySpendingPublisher(Protocol):
    """Publication seam for a formatted yen-only daily summary."""

    def publish(
        self,
        summary: DailySpendingSummary,
        *,
        target_channel: str,
    ) -> Awaitable[DiscordPublicationResult] | DiscordPublicationResult:
        """Publish a summary without coupling it to expense storage."""


@runtime_checkable
class CollectionLockLease(Protocol):
    """Handle returned by a lock provider for one collection key."""

    key: str
    expires_at: datetime

    def release(self) -> None:
        """Release the lease if it is still owned by this attempt."""


@runtime_checkable
class CollectionLock(Protocol):
    """Optional concurrency-control seam for one source/game collection."""

    def acquire(self, key: str, *, ttl: timedelta) -> CollectionLockLease | None:
        """Return a lease or ``None`` when another run owns the key."""
