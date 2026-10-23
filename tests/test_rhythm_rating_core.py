from datetime import UTC, date, datetime, timedelta

import pytest

from life_inbox.rhythm_rating import (
    ChangeKind,
    CollectionEventType,
    CollectionReason,
    CollectionState,
    CollectionStateMachine,
    DailySpendingSummary,
    DiscordPublicationPlan,
    DuplicateSkipReason,
    Game,
    InvalidCollectionTransition,
    ManualAction,
    RatingEntry,
    RatingSnapshot,
    RenderedImageMetadata,
    RenderedImageResult,
    build_dry_run_report,
    build_publication_plan,
    detect_change,
    format_daily_spending_message,
    japan_calendar_date,
)

UTC_NOON = datetime(2026, 8, 29, 3, 0, tzinfo=UTC)


def _entry(
    title: str,
    *,
    score: int = 1_000_000,
    frame_membership: str = "best",
) -> RatingEntry:
    return RatingEntry(
        game=Game.MAIMAI,
        title=title,
        rating=13.5,
        frame_membership=frame_membership,
        difficulty="Master",
        chart_type="standard",
        score=score,
        achievement="100.0000%",
        technical_score=1000,
        p_score=10,
        song_rating=13.5,
        completion_statuses=["FC", "clear"],
    )


def _snapshot(
    *,
    collected_at: datetime = UTC_NOON,
    generated_at: datetime | None = None,
    entries: list[RatingEntry] | None = None,
    snapshot_id: str | None = None,
) -> RatingSnapshot:
    return RatingSnapshot(
        game=Game.MAIMAI,
        entries=entries or [_entry("Song A"), _entry("Song B", frame_membership="recent")],
        rating=14.25,
        source="fixture-provider",
        collected_at=collected_at,
        generated_at=generated_at,
        snapshot_id=snapshot_id,
    )


def test_semantic_fingerprint_is_order_independent_and_excludes_collection_noise():
    first = _snapshot(
        collected_at=UTC_NOON,
        generated_at=datetime(2026, 8, 29, 3, 1, tzinfo=UTC),
        snapshot_id="random-a",
    )
    second = _snapshot(
        collected_at=UTC_NOON + timedelta(days=1),
        generated_at=datetime(2026, 8, 30, 3, 1, tzinfo=UTC),
        snapshot_id="random-b",
        entries=[_entry("Song B", frame_membership="recent"), _entry("Song A")],
    )

    assert first.semantic_fingerprint == second.semantic_fingerprint
    assert first.entries == second.entries
    assert first.semantic_fingerprint == first.fingerprint
    assert "random-a" not in first.semantic_fingerprint

    changed = _snapshot(entries=[_entry("Song A", score=999_999), _entry("Song B")])
    assert changed.semantic_fingerprint != first.semantic_fingerprint


def test_rating_entry_normalizes_values_and_is_immutable():
    entry = RatingEntry(
        game=" MAIMAI ",
        title="  Ｓｏｎｇ　A ",
        completion_status=" FC ",
        completion_statuses=[" FC ", "clear", "fc"],
    )

    assert entry.game is Game.MAIMAI
    assert entry.title == "Song A"
    assert entry.normalized_title == "song a"
    assert entry.completion_status == "fc"
    assert entry.completion_statuses == ("clear", "fc")
    with pytest.raises(AttributeError):
        entry.title = "other"


def test_change_detector_uses_semantics_before_timestamps_or_image_metadata():
    previous = _snapshot(collected_at=UTC_NOON)
    same_content = _snapshot(collected_at=UTC_NOON + timedelta(hours=1))
    unchanged = detect_change(
        same_content,
        previous,
        current_image_sha256="new-image",
        previous_image_sha256="old-image",
    )

    assert unchanged.kind is ChangeKind.UNCHANGED
    assert unchanged.changed is False
    assert unchanged.image_changed is True
    assert unchanged.duplicate_image is False
    assert unchanged.duplicate_skip_reason is DuplicateSkipReason.SEMANTIC_UNCHANGED

    changed = detect_change(
        _snapshot(entries=[_entry("Song A", score=999_999), _entry("Song B")]),
        previous,
        current_image_sha256="new-image",
        previous_image_sha256="old-image",
    )
    assert changed.kind is ChangeKind.CHANGED
    assert changed.changed is True
    assert changed.should_post is True


def test_same_image_sha_is_reported_as_a_deduplication_even_for_new_semantics():
    previous = _snapshot()
    current = _snapshot(entries=[_entry("Song A", score=999_999), _entry("Song B")])

    result = detect_change(
        current,
        previous,
        current_image_sha256="ABC123",
        previous_image_sha256="abc123",
    )

    assert result.kind is ChangeKind.DUPLICATE_IMAGE
    assert result.changed is True
    assert result.duplicate_image is True
    assert result.should_post is False
    assert result.duplicate_skip_reason is DuplicateSkipReason.IMAGE_SHA_ALREADY_SEEN


def test_state_machine_accepts_valid_route_and_rejects_invalid_route():
    machine = CollectionStateMachine(source="fixture", game=Game.MAIMAI)
    machine.transition(CollectionState.OPENING_PROVIDER)
    machine.transition(CollectionState.REFRESHING_SOURCE)
    machine.transition(CollectionState.COLLECTING_DATA)
    machine.transition(CollectionState.RENDERING_IMAGE)
    machine.transition(CollectionState.COMPARING_SNAPSHOT)
    machine.transition(CollectionState.READY_TO_POST)
    machine.transition(CollectionState.POSTING)
    event = machine.transition(CollectionState.POSTED)

    assert machine.state is CollectionState.POSTED
    assert machine.is_terminal is True
    assert event.previous_state is CollectionState.POSTING
    assert event.to_state is CollectionState.POSTED
    assert event.event_type is CollectionEventType.POSTED
    assert all(item.occurred_at.tzinfo is not None for item in machine.events)
    with pytest.raises(InvalidCollectionTransition):
        machine.transition(CollectionState.FAILED)

    with pytest.raises(ValueError, match="verified rendered image"):
        machine.result(snapshot=_snapshot())
    with pytest.raises(ValueError, match="verified rendered image"):
        machine.result(
            snapshot=_snapshot(),
            rendered_image=RenderedImageResult(
                success=True,
                metadata=RenderedImageMetadata(
                    path="rating.png",
                    sha256="not-a-sha",
                ),
            ),
        )
    successful = machine.result(
        snapshot=_snapshot(),
        rendered_image=RenderedImageResult(
            success=True,
            metadata=RenderedImageMetadata(
                path="rating.png",
                sha256="a" * 64,
                width=1,
                height=1,
                byte_size=16,
            ),
        ),
    )
    assert successful.success is True


def test_state_machine_does_not_auto_advance_after_manual_action_requirement():
    machine = CollectionStateMachine(source="fixture", game=Game.MAIMAI)
    machine.transition(CollectionState.OPENING_PROVIDER)
    machine.transition(
        CollectionState.AUTHENTICATION_REQUIRED,
        reason=CollectionReason.AUTHENTICATION_REQUIRED,
    )
    machine.transition(
        CollectionState.WAITING_FOR_MANUAL_ACTION,
        reason=CollectionReason.AUTHENTICATION_REQUIRED,
        manual_action=ManualAction.AUTHENTICATE,
    )

    assert machine.state is CollectionState.WAITING_FOR_MANUAL_ACTION
    assert machine.can_transition(CollectionState.REFRESHING_SOURCE) is False
    with pytest.raises(InvalidCollectionTransition):
        machine.transition(CollectionState.REFRESHING_SOURCE)
    assert machine.state is CollectionState.WAITING_FOR_MANUAL_ACTION

    confirmation = machine.confirm_manual_action(ManualAction.AUTHENTICATE)
    assert confirmation.explicit_confirmation is True
    assert confirmation.manual_action is ManualAction.AUTHENTICATE
    assert machine.state is CollectionState.REFRESHING_SOURCE


def test_timeout_and_failure_are_terminal_and_cannot_become_success():
    machine = CollectionStateMachine(source="fixture", game=Game.CHUNITHM)
    machine.transition(CollectionState.OPENING_PROVIDER)
    timeout_event = machine.timeout(message="fixture timed out")

    assert timeout_event.reason is CollectionReason.TIMEOUT
    assert machine.state is CollectionState.FAILED
    assert machine.result().success is False
    assert machine.result().failure_reason is CollectionReason.TIMEOUT
    with pytest.raises(InvalidCollectionTransition):
        machine.transition(CollectionState.POSTED)


def test_publication_plan_and_dry_run_report_are_secret_free_and_pure():
    current = _snapshot()
    plan = build_publication_plan(
        current,
        target_channel="rating-log",
        current_image=RenderedImageMetadata(
            path="data/rating.png",
            sha256="a" * 64,
            rendered_at=UTC_NOON,
        ),
        planned_message="maimai レーティング: 14.25",
    )
    assert isinstance(plan, DiscordPublicationPlan)
    assert plan.success is True
    assert plan.should_post is True
    assert plan.image_path == "data/rating.png"
    assert plan.semantic_fingerprint == current.semantic_fingerprint
    assert plan.target_channel == "rating-log"
    assert plan.planned_message == "maimai レーティング: 14.25"

    unchanged_plan = build_publication_plan(
        current,
        current,
        target_channel="rating-log",
        current_image=RenderedImageMetadata(
            path="data/rating-new.png",
            sha256="b" * 64,
            rendered_at=UTC_NOON,
        ),
    )
    assert unchanged_plan.should_post is False
    assert unchanged_plan.duplicate_skip_reason is DuplicateSkipReason.SEMANTIC_UNCHANGED
    report = build_dry_run_report([plan, unchanged_plan], generated_at=UTC_NOON)
    assert report.posts_planned == 1
    assert report.duplicate_skips == 1
    assert report.all_success is True

    failed_plan = build_publication_plan(
        None,
        source="fixture-provider",
        game=Game.MAIMAI,
        target_channel="rating-log",
        collection_success=False,
        failure_reason=CollectionReason.RATE_LIMITED,
    )
    assert failed_plan.success is False
    assert failed_plan.should_post is False
    assert failed_plan.failure_reason is CollectionReason.RATE_LIMITED
    assert failed_plan.semantic_fingerprint is None


def test_daily_spending_message_is_yen_only_and_handles_zero():
    summary = DailySpendingSummary(
        day=date(2026, 8, 29),
        total_yen=0,
        expense_count=0,
        category_totals={"食費": 0},
        generated_at=UTC_NOON,
    )

    message = format_daily_spending_message(summary)
    assert "2026年8月29日の支出" in message
    assert "合計: 0円（0件）" in message
    assert "食費: 0円" in message
    assert "cr" not in message.casefold()
    assert "円" in message


def test_daily_spending_formatter_excludes_machine_category_labels():
    summary = DailySpendingSummary(
        day=date(2026, 8, 29),
        total_yen=100,
        expense_count=1,
        category_totals={"自販機": 100, "食費": 0},
        generated_at=UTC_NOON,
    )

    message = format_daily_spending_message(summary)
    assert "自販機" not in message
    assert "食費: 0円" in message


def test_japan_date_boundary_uses_aware_utc_instants():
    just_before = datetime(2026, 8, 28, 14, 59, 59, 999999, tzinfo=UTC)
    at_boundary = datetime(2026, 8, 28, 15, 0, tzinfo=UTC)

    assert japan_calendar_date(just_before) == date(2026, 8, 28)
    assert japan_calendar_date(at_boundary) == date(2026, 8, 29)
    with pytest.raises(ValueError, match="timezone"):
        japan_calendar_date(datetime(2026, 8, 28, 15, 0))
