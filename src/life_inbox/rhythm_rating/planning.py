"""Pure publication plans and dry-run reports."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .change_detection import ChangeDetection, detect_change
from .enums import CollectionReason, DuplicateSkipReason, Game
from .models import RatingSnapshot, RatingValue, RenderedImageMetadata, RenderedImageResult
from .normalization import (
    ensure_aware_datetime,
    normalize_game,
    normalize_measure,
    normalize_optional_text,
    normalize_text,
    utc_now,
)


def _skip_reason(value: DuplicateSkipReason | str | None) -> DuplicateSkipReason | None:
    if value is None or isinstance(value, DuplicateSkipReason):
        return value
    try:
        return DuplicateSkipReason(str(value).strip().casefold())
    except ValueError as exc:
        raise ValueError("unsupported duplicate skip reason") from exc


def _reason(value: CollectionReason | str | None) -> CollectionReason | None:
    if value is None or isinstance(value, CollectionReason):
        return value
    try:
        return CollectionReason(str(value).strip().casefold())
    except ValueError as exc:
        raise ValueError("unsupported collection reason") from exc


def _image_path(
    value: str | Path | RenderedImageMetadata | RenderedImageResult | None,
) -> str | None:
    if isinstance(value, RenderedImageMetadata):
        return value.image_path
    if isinstance(value, RenderedImageResult):
        return value.image_path
    if value is None:
        return None
    return normalize_text(str(value), field_name="image_path")


def _verified_image_sha(
    value: str | Path | RenderedImageMetadata | RenderedImageResult | None,
) -> str | None:
    if isinstance(value, RenderedImageResult):
        if not value.success or value.metadata is None:
            return None
        candidate = value.metadata.image_sha256
    elif isinstance(value, RenderedImageMetadata):
        candidate = value.image_sha256
    else:
        # A bare path cannot prove rendering success or content identity.
        return None
    normalized = normalize_optional_text(candidate, field_name="image_sha256", casefold=True)
    if normalized is None or len(normalized) != 64:
        return None
    if any(character not in "0123456789abcdef" for character in normalized):
        return None
    return normalized


def _numeric_delta(current: RatingValue, previous: RatingValue) -> RatingValue:
    if (
        isinstance(current, (int, float))
        and not isinstance(current, bool)
        and isinstance(previous, (int, float))
        and not isinstance(previous, bool)
    ):
        value = current - previous
        return int(value) if isinstance(value, float) and value.is_integer() else value
    return None


def _rating_label(value: RatingValue) -> str:
    return "未取得" if value is None else str(value)


@dataclass(frozen=True, slots=True)
class DiscordPublicationPlan:
    """A secret-free, side-effect-free description of one possible post."""

    source: str
    game: Game | str
    success: bool
    current_rating: RatingValue = None
    previous_rating: RatingValue = None
    delta: RatingValue = None
    semantic_fingerprint: str | None = None
    image_path: str | Path | None = None
    target_channel: str = ""
    planned_message: str = ""
    duplicate_skip_reason: DuplicateSkipReason | str | None = None
    failure_reason: CollectionReason | str | None = None
    should_post: bool = False
    dry_run: bool = True

    def __post_init__(self) -> None:
        normalized_success = self.success
        if type(normalized_success) is not bool:
            raise ValueError("success must be a boolean")
        if type(self.should_post) is not bool:
            raise ValueError("should_post must be a boolean")
        if type(self.dry_run) is not bool:
            raise ValueError("dry_run must be a boolean")
        normalized_reason = _reason(self.failure_reason)
        if normalized_success and normalized_reason is not None:
            raise ValueError("a successful plan cannot have a failure reason")
        object.__setattr__(self, "source", normalize_text(self.source, field_name="source"))
        object.__setattr__(self, "game", normalize_game(self.game))
        for field_name in (
            "current_rating",
            "previous_rating",
            "delta",
        ):
            object.__setattr__(
                self,
                field_name,
                normalize_measure(getattr(self, field_name), field_name=field_name),
            )
        object.__setattr__(
            self,
            "semantic_fingerprint",
            normalize_optional_text(
                self.semantic_fingerprint,
                field_name="semantic_fingerprint",
                casefold=True,
            ),
        )
        object.__setattr__(self, "image_path", _image_path(self.image_path))
        object.__setattr__(
            self,
            "target_channel",
            normalize_text(self.target_channel, field_name="target_channel"),
        )
        message = normalize_optional_text(self.planned_message, field_name="planned_message")
        object.__setattr__(self, "planned_message", message or "")
        object.__setattr__(self, "duplicate_skip_reason", _skip_reason(self.duplicate_skip_reason))
        object.__setattr__(self, "failure_reason", normalized_reason)

    @property
    def is_duplicate_skip(self) -> bool:
        return self.duplicate_skip_reason is not None and not self.should_post


@dataclass(frozen=True, slots=True)
class DiscordPublicationResult:
    """Detached outcome of executing a publication plan."""

    plan: DiscordPublicationPlan
    success: bool
    posted: bool = False
    message_id: str | None = None
    published_at: datetime | str | None = None
    failure_reason: CollectionReason | str | None = None
    duplicate_skip_reason: DuplicateSkipReason | str | None = None

    def __post_init__(self) -> None:
        if type(self.success) is not bool or type(self.posted) is not bool:
            raise ValueError("success and posted must be booleans")
        if self.posted and not self.success:
            raise ValueError("a posted result must be successful")
        if self.posted and not self.plan.should_post:
            raise ValueError("a skipped plan cannot be posted")
        object.__setattr__(
            self,
            "message_id",
            normalize_optional_text(self.message_id, field_name="message_id"),
        )
        object.__setattr__(self, "failure_reason", _reason(self.failure_reason))
        object.__setattr__(
            self,
            "duplicate_skip_reason",
            _skip_reason(self.duplicate_skip_reason),
        )
        if self.published_at is not None:
            object.__setattr__(
                self,
                "published_at",
                ensure_aware_datetime(self.published_at, field_name="published_at"),
            )


@dataclass(frozen=True, slots=True)
class DryRunReport:
    """Read-only collection of plans suitable for logs or review UI."""

    plans: Iterable[DiscordPublicationPlan] = ()
    generated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "plans", tuple(self.plans))
        object.__setattr__(
            self,
            "generated_at",
            ensure_aware_datetime(self.generated_at, field_name="generated_at"),
        )

    @property
    def all_success(self) -> bool:
        return all(plan.success for plan in self.plans)

    @property
    def posts_planned(self) -> int:
        return sum(plan.should_post for plan in self.plans)

    @property
    def duplicate_skips(self) -> int:
        return sum(plan.is_duplicate_skip for plan in self.plans)


def build_publication_plan(
    current: RatingSnapshot | None,
    previous: RatingSnapshot | None = None,
    *,
    source: str | None = None,
    game: Game | str | None = None,
    current_image: str | Path | RenderedImageMetadata | RenderedImageResult | None = None,
    previous_image_sha256: str | RenderedImageMetadata | RenderedImageResult | None = None,
    target_channel: str,
    planned_message: str | None = None,
    collection_success: bool = True,
    failure_reason: CollectionReason | str | None = None,
    dry_run: bool = True,
) -> DiscordPublicationPlan:
    """Create a safe plan from current/previous snapshots without side effects."""

    if type(collection_success) is not bool or type(dry_run) is not bool:
        raise ValueError("collection_success and dry_run must be booleans")
    if current is None:
        if previous is not None:
            resolved_game = previous.game
        elif game is not None:
            resolved_game = normalize_game(game)
        else:
            raise ValueError("a failed plan requires an explicit game")
        current_rating = None
        previous_rating = previous.rating if previous is not None else None
        fingerprint = None
        detection: ChangeDetection | None = None
    else:
        resolved_game = current.game
        current_rating = current.rating
        previous_rating = previous.rating if previous is not None else None
        fingerprint = current.semantic_fingerprint
        verified_image_sha = _verified_image_sha(current_image)
        detection = detect_change(
            current,
            previous,
            current_image_sha256=verified_image_sha,
            previous_image_sha256=previous_image_sha256,
        )
    if current is None:
        verified_image_sha = None
    normalized_failure = _reason(failure_reason)
    if collection_success and normalized_failure is not None:
        raise ValueError("a successful collection cannot have a failure reason")
    if collection_success and current is not None and verified_image_sha is None:
        normalized_failure = CollectionReason.IMAGE_MISSING
    success = (
        collection_success
        and current is not None
        and normalized_failure is None
        and verified_image_sha is not None
    )
    should_post = bool(success and detection is not None and detection.should_post)
    skip_reason = (
        detection.duplicate_skip_reason
        if detection is not None and not should_post
        else None
    )
    if normalized_failure is not None:
        default_message = "レーティング取得に失敗しました。"
    elif current is None:
        default_message = "レーティング結果を取得できませんでした。"
    elif normalized_failure is CollectionReason.IMAGE_MISSING:
        default_message = "レーティングデータは取得しましたが、検証済み画像がありません。"
    elif detection is not None and detection.duplicate_image:
        default_message = (
            f"{resolved_game.value} のデータは変化しましたが、画像が前回と同一のため投稿しません。"
        )
    elif not should_post:
        default_message = f"{resolved_game.value} のレーティングに変更はありません。"
    else:
        delta = _numeric_delta(current_rating, previous_rating)
        delta_label = "" if delta is None else f"（差分 {delta:+g}）"
        default_message = (
            f"{resolved_game.value} レーティング: {_rating_label(current_rating)}{delta_label}"
        )
    resolved_source = source or (
        current.source
        if current is not None
        else previous.source
        if previous is not None
        else "unknown"
    )
    return DiscordPublicationPlan(
        source=resolved_source,
        game=resolved_game,
        success=success,
        current_rating=current_rating,
        previous_rating=previous_rating,
        delta=_numeric_delta(current_rating, previous_rating),
        semantic_fingerprint=fingerprint,
        image_path=_image_path(current_image),
        target_channel=target_channel,
        planned_message=planned_message or default_message,
        duplicate_skip_reason=skip_reason,
        failure_reason=normalized_failure,
        should_post=should_post,
        dry_run=dry_run,
    )


def build_dry_run_report(
    plans: Iterable[DiscordPublicationPlan],
    *,
    generated_at: datetime | str | None = None,
) -> DryRunReport:
    """Wrap plans in a deterministic, timestamped review structure."""

    return DryRunReport(plans=tuple(plans), generated_at=generated_at or utc_now())


PublicationPlan = DiscordPublicationPlan
PublicationResult = DiscordPublicationResult
