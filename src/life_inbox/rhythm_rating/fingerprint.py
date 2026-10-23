"""Semantic snapshot identity independent of collection and rendering noise."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from typing import Any

from .models import RatingEntry, RatingSnapshot
from .normalization import canonical_json, normalize_game


def canonical_snapshot_payload(snapshot: RatingSnapshot) -> dict[str, object]:
    """Return the exact payload used for semantic fingerprinting."""

    if not isinstance(snapshot, RatingSnapshot):
        raise TypeError("snapshot must be a RatingSnapshot")
    return snapshot.semantic_payload()


def canonical_serialize(value: Any) -> str:
    """Serialize canonical JSON content with stable key and list formatting."""

    return canonical_json(value)


def semantic_fingerprint(snapshot: RatingSnapshot) -> str:
    """Hash the semantic content of a rating snapshot with SHA-256.

    ``RatingSnapshot`` normalizes and sorts entries before this function is
    called.  Timestamps, generated IDs, image metadata, and compression are
    deliberately absent from the payload.
    """

    canonical = canonical_serialize(canonical_snapshot_payload(snapshot))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def fingerprint_entries(
    entries: Iterable[RatingEntry | Mapping[str, object]],
    *,
    game: str,
    rating: int | float | str | None = None,
) -> str:
    """Convenience seam for adapters that have rows before making a snapshot."""

    snapshot = RatingSnapshot(game=normalize_game(game), entries=entries, rating=rating)
    return semantic_fingerprint(snapshot)


# Names used by different integration layers can point to the same pure
# implementation without introducing provider-specific adapters here.
generate_semantic_fingerprint = semantic_fingerprint
fingerprint_snapshot = semantic_fingerprint
