"""Enumerations for the provider-independent rhythm-rating domain."""

from __future__ import annotations

from enum import StrEnum


class Game(StrEnum):
    """Supported rhythm games."""

    CHUNITHM = "chunithm"
    MAIMAI = "maimai"
    ONGEKI = "ongeki"


# ``RhythmGame`` is a convenient name at integration boundaries and mirrors
# the existing application's rhythm terminology.
RhythmGame = Game
GameType = Game


class CollectionState(StrEnum):
    """Observable states in one rating collection attempt."""

    QUEUED = "queued"
    OPENING_PROVIDER = "openingProvider"
    AUTHENTICATION_REQUIRED = "authenticationRequired"
    WAITING_FOR_MANUAL_ACTION = "waitingForManualAction"
    REFRESHING_SOURCE = "refreshingSource"
    COLLECTING_DATA = "collectingData"
    RENDERING_IMAGE = "renderingImage"
    COMPARING_SNAPSHOT = "comparingSnapshot"
    READY_TO_POST = "readyToPost"
    POSTING = "posting"
    POSTED = "posted"
    UNCHANGED = "unchanged"
    FAILED = "failed"
    CANCELLED = "cancelled"


RatingCollectionState = CollectionState


class CollectionReason(StrEnum):
    """Stable, non-secret reason codes for collection outcomes."""

    AUTHENTICATION_REQUIRED = "authentication_required"
    CAPTCHA_REQUIRED = "captcha_required"
    TERMS_CONFIRMATION_REQUIRED = "terms_confirmation_required"
    SUBSCRIPTION_MISSING = "subscription_missing"
    WRONG_PAGE = "wrong_page"
    BOOKMARKLET_FAILURE = "bookmarklet_failure"
    IMAGE_MISSING = "image_missing"
    DOM_MISMATCH = "dom_mismatch"
    USER_NOT_FOUND = "user_not_found"
    PRIVATE_MODE = "private_mode"
    PROVIDER_NETWORK_OUTAGE = "provider_network_outage"
    INVALID_RESPONSE = "invalid_response"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    MANUAL_CONFIRMATION_REQUIRED = "manual_confirmation_required"
    PUBLISH_FAILURE = "publish_failure"
    UNKNOWN = "unknown"

    # Short aliases keep adapter code readable while retaining one stable
    # serialized reason value for repositories and reports.
    AUTH_REQUIRED = "authentication_required"
    CAPTCHA = "captcha_required"
    TERMS_CONFIRMATION = "terms_confirmation_required"
    PROVIDER_OUTAGE = "provider_network_outage"
    INVALID_PROVIDER_RESPONSE = "invalid_response"
    RATE_LIMIT = "rate_limited"


# These aliases make the classification discoverable under the names most
# likely to be used by provider and scheduler integrations.
CollectionErrorCode = CollectionReason
CollectionError = CollectionReason
FailureReason = CollectionReason
CollectionFailureReason = CollectionReason


class ManualAction(StrEnum):
    """Actions that must be confirmed by a user or browser operator."""

    AUTHENTICATE = "authenticate"
    SOLVE_CAPTCHA = "solve_captcha"
    CONFIRM_TERMS = "confirm_terms"
    ADD_SUBSCRIPTION = "add_subscription"
    OPEN_CORRECT_PAGE = "open_correct_page"
    RETRY_BOOKMARKLET = "retry_bookmarklet"
    PROVIDE_IMAGE = "provide_image"
    REPAIR_DOM = "repair_dom"
    SELECT_USER = "select_user"
    DISABLE_PRIVATE_MODE = "disable_private_mode"

    CAPTCHA = "solve_captcha"
    TERMS_CONFIRMATION = "confirm_terms"
    SUBSCRIPTION = "add_subscription"


ManualActionKind = ManualAction
ManualActionType = ManualAction


class CollectionEventType(StrEnum):
    """Meaning of a state-machine domain event."""

    STATE_TRANSITION = "state_transition"
    AUTHENTICATION_REQUIRED = "authentication_required"
    MANUAL_ACTION_REQUIRED = "manual_action_required"
    MANUAL_ACTION_CONFIRMED = "manual_action_confirmed"
    PROVIDER_OPENED = "provider_opened"
    SOURCE_REFRESHED = "source_refreshed"
    DATA_COLLECTED = "data_collected"
    IMAGE_RENDERED = "image_rendered"
    SNAPSHOT_COMPARED = "snapshot_compared"
    POST_READY = "post_ready"
    POSTED = "posted"
    UNCHANGED = "unchanged"
    FAILURE = "failure"
    CANCELLED = "cancelled"


class ChangeKind(StrEnum):
    """Result categories emitted by the pure snapshot comparator."""

    NEW = "new"
    CHANGED = "changed"
    UNCHANGED = "unchanged"
    DUPLICATE_IMAGE = "duplicate_image"


class DuplicateSkipReason(StrEnum):
    """Reasons a dry-run planner can avoid a Discord publication."""

    SEMANTIC_UNCHANGED = "semantic_unchanged"
    IMAGE_SHA_ALREADY_SEEN = "image_sha_already_seen"
    ALREADY_POSTED = "already_posted"
