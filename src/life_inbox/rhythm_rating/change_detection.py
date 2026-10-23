"""Pure semantic and rendered-image change detection."""

from __future__ import annotations

from dataclasses import dataclass

from .enums import ChangeKind, DuplicateSkipReason
from .models import RatingSnapshot, RenderedImageMetadata, RenderedImageResult
from .normalization import normalize_optional_text


def _image_sha(value: str | None) -> str | None:
    return normalize_optional_text(value, field_name="image_sha256", casefold=True)


@dataclass(frozen=True, slots=True)
class ChangeDetection:
    """Comparison result used by publication planning and repositories."""

    kind: ChangeKind
    current_fingerprint: str
    previous_fingerprint: str | None
    semantic_changed: bool
    image_changed: bool
    duplicate_image: bool
    duplicate_skip_reason: DuplicateSkipReason | None = None

    @property
    def changed(self) -> bool:
        return self.semantic_changed

    @property
    def unchanged(self) -> bool:
        return not self.semantic_changed

    @property
    def is_changed(self) -> bool:
        return self.changed

    @property
    def is_unchanged(self) -> bool:
        return self.unchanged

    @property
    def should_post(self) -> bool:
        # A semantic change accompanied by byte-identical imagery is still
        # persisted, but the stale/reused image must not be posted as if it
        # were newly generated.
        return self.changed and not self.duplicate_image


def _metadata_sha(
    value: str | RenderedImageMetadata | RenderedImageResult | None,
) -> str | None:
    if isinstance(value, RenderedImageMetadata):
        return value.image_sha256
    if isinstance(value, RenderedImageResult):
        return value.image_sha256
    return _image_sha(value)


def detect_change(
    current: RatingSnapshot,
    previous: RatingSnapshot | None = None,
    *,
    current_image_sha256: str | RenderedImageMetadata | RenderedImageResult | None = None,
    previous_image_sha256: str | RenderedImageMetadata | RenderedImageResult | None = None,
) -> ChangeDetection:
    """Compare semantic content, optionally recording image deduplication.

    Semantic content is authoritative: a new collection timestamp does not
    count as a change.  A repeated image SHA is classified separately so the
    structured snapshot can be retained while a stale image is not posted as
    a new result.
    """

    current_fingerprint = current.semantic_fingerprint
    previous_fingerprint = previous.semantic_fingerprint if previous is not None else None
    current_sha = _metadata_sha(current_image_sha256)
    previous_sha = _metadata_sha(previous_image_sha256)
    semantic_changed = previous is None or current_fingerprint != previous_fingerprint
    image_changed = current_sha != previous_sha
    duplicate_image = (
        current_sha is not None and previous_sha is not None and current_sha == previous_sha
    )
    if previous is None:
        kind = ChangeKind.NEW
        skip_reason = None
    elif not semantic_changed:
        kind = ChangeKind.UNCHANGED
        skip_reason = DuplicateSkipReason.SEMANTIC_UNCHANGED
    elif duplicate_image:
        kind = ChangeKind.DUPLICATE_IMAGE
        skip_reason = DuplicateSkipReason.IMAGE_SHA_ALREADY_SEEN
    else:
        kind = ChangeKind.CHANGED
        skip_reason = None
    return ChangeDetection(
        kind=kind,
        current_fingerprint=current_fingerprint,
        previous_fingerprint=previous_fingerprint,
        semantic_changed=semantic_changed,
        image_changed=image_changed,
        duplicate_image=duplicate_image,
        duplicate_skip_reason=skip_reason,
    )


detect_rating_change = detect_change


class PureRatingChangeDetector:
    """Stateless object adapter for the ``RatingChangeDetector`` protocol."""

    @staticmethod
    def compare(
        current: RatingSnapshot,
        previous: RatingSnapshot | None = None,
        *,
        current_image_sha256: str | RenderedImageMetadata | RenderedImageResult | None = None,
        previous_image_sha256: str | RenderedImageMetadata | RenderedImageResult | None = None,
    ) -> ChangeDetection:
        return detect_change(
            current,
            previous,
            current_image_sha256=current_image_sha256,
            previous_image_sha256=previous_image_sha256,
        )


RatingChangeDetectorImplementation = PureRatingChangeDetector
